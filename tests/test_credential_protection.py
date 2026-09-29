"""Synthetic-only credential storage tests: no user state, accounts, or network."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jarvis import credential_protection as protection
from jarvis.connections import ConnectionManager
from jarvis.openrouter import KeyStore

SYNTHETIC_KEY = "sk-or-" + "synthetic_fixture_" * 3
SYNTHETIC_TOKEN = "fixture-connector-secret-not-real"


class ProtectedStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Simulates separate OS identities; actual AES-GCM and all filesystem code
        # remain real. A separate Windows test exercises the native DPAPI binding.
        self.identity_key = AESGCM.generate_key(bit_length=256)

        def fake_dpapi(value, entropy, *, decrypt=False):
            cipher = AESGCM(self.identity_key)
            if decrypt:
                return cipher.decrypt(value[:12], value[12:], entropy)
            nonce = os.urandom(12)
            return nonce + cipher.encrypt(nonce, value, entropy)

        patcher = mock.patch.object(protection, "_dpapi", side_effect=fake_dpapi)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.root / "private" / "secret"
        self.store = protection.ProtectedFile(self.path, "fixture")

    def test_ciphertext_and_scratch_never_contain_plaintext(self):
        replace = os.replace
        seen = []

        def inspect(source, destination):
            data = Path(source).read_bytes()
            self.assertTrue(data.startswith(protection.MAGIC))
            self.assertNotIn(SYNTHETIC_TOKEN.encode(), data)
            seen.append(data)
            return replace(source, destination)

        with mock.patch.object(protection.os, "replace", side_effect=inspect):
            self.store.write(SYNTHETIC_TOKEN.encode())
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.path.read_bytes(), seen[0])
        reopened = protection.ProtectedFile(self.path, "fixture")
        self.assertEqual(reopened.read(lambda _: False), SYNTHETIC_TOKEN.encode())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_repeated_writes_use_distinct_ciphertexts(self):
        self.store.write(b"synthetic")
        first = self.path.read_bytes()
        self.store.write(b"synthetic")
        self.assertNotEqual(self.path.read_bytes(), first)

    def test_tampering_and_truncation_fail_closed(self):
        self.store.write(b"synthetic")
        original = self.path.read_bytes()
        for bad in (original[:-1], original[:20], original[:-1] + bytes([original[-1] ^ 1]), b"garbage"):
            self.path.write_bytes(bad)
            with self.assertRaises(protection.CredentialProtectionError):
                self.store.read(lambda _: False)

    def test_identity_and_purpose_are_independently_bound(self):
        self.store.write(b"synthetic")
        with self.assertRaises(protection.CredentialProtectionError):
            protection.ProtectedFile(self.path, "different-purpose").read(lambda _: False)
        self.identity_key = AESGCM.generate_key(bit_length=256)
        with self.assertRaises(protection.CredentialProtectionError):
            self.store.read(lambda _: False)

    def test_copied_credentials_cannot_be_used_at_another_path(self):
        self.store.write(b"synthetic")
        other = self.root / "other"
        other.write_bytes(self.path.read_bytes())
        with self.assertRaises(protection.CredentialProtectionError):
            protection.ProtectedFile(other, "fixture").read(lambda _: False)

    def test_protection_failure_preserves_legacy_without_scratch(self):
        self.path.parent.mkdir()
        original = SYNTHETIC_TOKEN.encode()
        self.path.write_bytes(original)
        with mock.patch.object(protection, "_dpapi", side_effect=OSError("fixture failure")):
            with self.assertRaises(protection.CredentialProtectionError) as caught:
                self.store.read(lambda value: value == original)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_replace_failure_preserves_legacy_and_cleans_ciphertext_scratch(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"synthetic legacy")
        with mock.patch.object(protection.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(protection.CredentialProtectionError):
                self.store.read(lambda _: True)
        self.assertEqual(self.path.read_bytes(), b"synthetic legacy")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_failed_write_preserves_previous_encrypted_file(self):
        self.store.write(b"old synthetic value")
        original = self.path.read_bytes()
        with mock.patch.object(protection, "_dpapi", side_effect=OSError()):
            with self.assertRaises(protection.CredentialProtectionError):
                self.store.write(b"new synthetic value")
        self.assertEqual(self.path.read_bytes(), original)

    def test_short_or_corrupt_scratch_write_preserves_legacy(self):
        self.path.parent.mkdir()
        original = b"synthetic legacy"
        fdopen = os.fdopen

        class FaultyStream:
            def __init__(self, stream, report_short):
                self.stream, self.report_short = stream, report_short

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def write(self, value):
                written = self.stream.write(value[:len(value) // 2])
                return written if self.report_short else len(value)

        for report_short in (True, False):
            self.path.write_bytes(original)

            def faulty_open(fd, mode, report_short=report_short):
                stream = fdopen(fd, mode)
                return FaultyStream(stream, report_short) if mode == "w+b" else stream

            with mock.patch.object(protection.os, "fdopen", side_effect=faulty_open):
                with self.assertRaises(protection.CredentialProtectionError):
                    self.store.read(lambda value: value == original)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_parent_alias_is_rejected(self):
        self.path.parent.mkdir()
        aliased = self.path.parent / ".." / self.path.parent.name / self.path.name
        with self.assertRaises(protection.CredentialProtectionError):
            protection.ProtectedFile(aliased, "fixture").write(b"synthetic")

    def test_failed_roundtrip_never_creates_file(self):
        with mock.patch.object(self.store, "_open", return_value=b"wrong"):
            with self.assertRaises(protection.CredentialProtectionError):
                self.store.write(b"synthetic")
        self.assertFalse(self.path.exists())

    def test_temp_collision_is_not_deleted(self):
        self.path.parent.mkdir()
        collision = self.path.with_name(self.path.name + ".fixed.tmp")
        collision.write_bytes(b"unrelated fixture")
        with mock.patch.object(protection.secrets, "token_hex", return_value="fixed"):
            with self.assertRaises(protection.CredentialProtectionError):
                self.store.write(b"synthetic")
        self.assertEqual(collision.read_bytes(), b"unrelated fixture")

    def test_hardlinked_file_is_rejected_for_read_write_and_clear(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"synthetic")
        other = self.root / "linked"
        os.link(self.path, other)
        for operation in (lambda: self.store.read(lambda _: True), lambda: self.store.write(b"new"), self.store.clear):
            with self.assertRaises(protection.CredentialProtectionError):
                operation()
        self.assertEqual(other.read_bytes(), b"synthetic")

    def test_directory_in_place_of_file_is_rejected(self):
        self.path.mkdir(parents=True)
        with self.assertRaises(protection.CredentialProtectionError):
            self.store.write(b"synthetic")

    def test_reparse_point_is_not_an_ordinary_directory(self):
        info = mock.Mock(st_mode=0o040700, st_file_attributes=0x400)
        self.assertFalse(protection._ordinary(info, directory=True))

    def test_oversized_file_is_not_loaded(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"x" * (protection.MAX_BYTES + 1))
        with self.assertRaises(protection.CredentialProtectionError):
            self.store.read(lambda _: True)

    def test_connector_legacy_migrates_and_connection_id_is_bound(self):
        manager = ConnectionManager(self.root)
        cid = "conn_123456abcdef"
        path = manager._secret_path(cid)
        values = {"token": SYNTHETIC_TOKEN, "env": {"FIXTURE": "synthetic-env"}}
        path.write_text(json.dumps(values), encoding="utf-8")
        self.assertEqual(manager._secrets(cid), values)
        self.assertTrue(path.read_bytes().startswith(protection.MAGIC))
        self.assertNotIn(SYNTHETIC_TOKEN.encode(), path.read_bytes())
        other = "conn_abcdef123456"
        manager._secret_path(other).write_bytes(path.read_bytes())
        with self.assertRaises(protection.CredentialProtectionError):
            manager._secrets(other)

    def test_connector_invalid_legacy_does_not_become_empty_credentials(self):
        manager = ConnectionManager(self.root)
        cid = "conn_123456abcdef"
        manager._secret_path(cid).write_text("[]", encoding="utf-8")
        with self.assertRaises(protection.CredentialProtectionError):
            manager._secrets(cid)

    def test_connector_add_protection_failure_does_not_publish_config(self):
        manager = ConnectionManager(self.root)
        with mock.patch.object(protection, "_dpapi", side_effect=OSError()):
            with self.assertRaises(protection.CredentialProtectionError):
                manager.add({"preset": "github", "token": SYNTHETIC_TOKEN})
        self.assertEqual(manager.list(), [])
        self.assertEqual(list((manager.root / "secrets").iterdir()), [])

    def test_openrouter_legacy_migration_reopen_and_ciphertext(self):
        store = KeyStore(self.root)
        store.path.parent.mkdir()
        store.path.write_text(SYNTHETIC_KEY, encoding="utf-8")
        self.assertEqual(store.get(), SYNTHETIC_KEY)
        self.assertNotIn(SYNTHETIC_KEY.encode(), store.path.read_bytes())
        self.assertEqual(KeyStore(self.root).get(), SYNTHETIC_KEY)
        self.assertEqual(store.source(), "hub")

    def test_openrouter_corrupt_file_does_not_fallback_to_environment(self):
        store = KeyStore(self.root)
        store.set(SYNTHETIC_KEY)
        store.path.write_bytes(b"invalid fixture")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": SYNTHETIC_KEY}):
            with self.assertRaises(protection.CredentialProtectionError):
                store.get()
            with self.assertRaises(protection.CredentialProtectionError):
                store.source()

    def test_openrouter_failed_migration_preserves_legacy(self):
        store = KeyStore(self.root)
        store.path.parent.mkdir()
        store.path.write_text(SYNTHETIC_KEY, encoding="utf-8")
        with mock.patch.object(protection, "_dpapi", side_effect=OSError()):
            with self.assertRaises(protection.CredentialProtectionError):
                store.get()
        self.assertEqual(store.path.read_text(), SYNTHETIC_KEY)


class NativeProtectionTests(unittest.TestCase):
    def test_unsupported_platform_fails_closed(self):
        with mock.patch.object(protection.os, "name", "unsupported"):
            with self.assertRaises(protection.CredentialProtectionError):
                protection._dpapi(b"synthetic", b"fixture")

    @unittest.skipUnless(os.name == "nt", "native per-user DPAPI requires Windows")
    def test_native_dpapi_roundtrip_and_entropy_binding(self):
        wrapped = protection._dpapi(b"synthetic DPAPI fixture", b"fixture-one")
        self.assertNotIn(b"synthetic DPAPI fixture", wrapped)
        self.assertEqual(protection._dpapi(wrapped, b"fixture-one", decrypt=True), b"synthetic DPAPI fixture")
        with self.assertRaises(protection.CredentialProtectionError):
            protection._dpapi(wrapped, b"fixture-two", decrypt=True)

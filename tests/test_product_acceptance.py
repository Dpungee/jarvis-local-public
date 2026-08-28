from __future__ import annotations

import unittest

from jarvis.agent import (
    _product_comparison_acceptance_failure,
    _verified_product_comparison,
)


def _comparison(
    count: int,
    *,
    price: str = "$149.00",
    specs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "ranking": "ranked",
        "products": [
            {
                "name": f"Office Chair {index}",
                "price_text": price,
                "key_specs": list(specs or []),
            }
            for index in range(count)
        ],
    }


class ProductAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _raw_card(
        *,
        url: str,
        price: str | None,
        currency: str | None,
        source_kind: str = "other",
        seller: str | None = None,
        manufacturer: str | None = None,
    ) -> dict[str, object]:
        return {
            "ranking": "ranked",
            "products": [{
                "name": "Exact Office Chair",
                "source_url": url,
                "source_kind": source_kind,
                "seller": seller,
                "manufacturer": manufacturer,
                "price_text": price,
                "currency": currency,
                "availability": "In stock",
                "key_specs": ["mesh"],
                "why_fit": "Matches the requested upholstery.",
                "tradeoff": "No durability evidence.",
            }],
        }

    def test_verified_price_never_accepts_a_numeric_substring(self):
        url = "https://shop.example/exact-office-chair"
        page = {
            "url": url,
            "title": "Exact Office Chair",
            "content": "Exact Office Chair costs $199.99 USD and is In stock with mesh upholstery.",
        }
        comparison = _verified_product_comparison(
            self._raw_card(url=url, price="$19", currency="USD"),
            {url: page},
        )

        self.assertIsNotNone(comparison)
        self.assertIsNone(comparison["products"][0]["price_text"])
        self.assertIn(
            "price ceiling could not be verified",
            _product_comparison_acceptance_failure(
                "Is this mesh office chair under $50?",
                comparison,
            ),
        )

    def test_exact_price_and_currency_are_derived_from_page_tokens(self):
        url = "https://example-furnishings.example/products/exact-office-chair"
        page = {
            "url": url,
            "title": "Exact Office Chair",
            "content": (
                "Exact Office Chair by Example Furnishings is In stock, mesh, and priced at "
                "$129.99 USD."
            ),
        }
        comparison = _verified_product_comparison(
            self._raw_card(
                url=url,
                price="$129.99",
                currency="USD",
                source_kind="manufacturer",
                seller="Example Furnishings",
                manufacturer="Example Furnishings",
            ),
            {url: page},
        )

        product = comparison["products"][0]
        self.assertEqual(product["price_text"], "$129.99")
        self.assertEqual(product["currency"], "USD")
        self.assertEqual(product["source_kind"], "manufacturer")

    def test_missing_price_and_currency_remain_unavailable(self):
        url = "https://shop.example/exact-office-chair"
        page = {
            "url": url,
            "title": "Exact Office Chair",
            "content": "Exact Office Chair is In stock with mesh upholstery. Contact us for pricing.",
        }
        comparison = _verified_product_comparison(
            self._raw_card(url=url, price=None, currency=None),
            {url: page},
        )

        product = comparison["products"][0]
        self.assertIsNone(product["price_text"])
        self.assertIsNone(product["currency"])

    def test_conflicting_seller_manufacturer_source_label_voids_card(self):
        url = "https://retailer.example/products/exact-office-chair"
        page = {
            "url": url,
            "title": "Exact Office Chair",
            "content": (
                "Exact Office Chair by Example Furnishings. Sold by Example Retailer. "
                "$129.99 USD. In stock with mesh upholstery."
            ),
        }
        mislabeled = _verified_product_comparison(
            self._raw_card(
                url=url,
                price="$129.99",
                currency="USD",
                source_kind="manufacturer",
                seller="Example Retailer",
                manufacturer="Example Furnishings",
            ),
            {url: page},
        )
        valid = _verified_product_comparison(
            self._raw_card(
                url=url,
                price="$129.99",
                currency="USD",
                source_kind="seller",
                seller="Example Retailer",
                manufacturer="Example Furnishings",
            ),
            {url: page},
        )

        self.assertIsNone(mislabeled)
        self.assertEqual(valid["products"][0]["source_kind"], "seller")

    def test_conversational_filler_and_optional_preferences_are_not_hard_specs(self):
        prompt = (
            "Hey, could you please show me the top 4 office chairs that are good for "
            "everyday work? A headrest would be nice but is not required."
        )
        self.assertIsNone(
            _product_comparison_acceptance_failure(prompt, _comparison(4))
        )

    def test_top_count_is_enforced_for_numeric_and_word_forms(self):
        for prompt in (
            "Show me the top 4 office chairs.",
            "Show me the top four office chairs.",
        ):
            with self.subTest(prompt=prompt):
                failure = _product_comparison_acceptance_failure(
                    prompt,
                    _comparison(3),
                )
                self.assertIsNotNone(failure)
                self.assertIn("at least 4", failure)

    def test_natural_budget_ceiling_forms_are_enforced(self):
        prompts = (
            "Find me 3 office chairs; my budget is $150.",
            "Find me 3 office chairs at $150 or less.",
            "Find me 3 office chairs and don't spend more than $150.",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(
                    _product_comparison_acceptance_failure(
                        prompt,
                        _comparison(3, price="$150.00"),
                    )
                )
                failure = _product_comparison_acceptance_failure(
                    prompt,
                    _comparison(3, price="$151.00"),
                )
                self.assertIsNotNone(failure)
                self.assertIn("above", failure)

    def test_true_hard_requirements_remain_strict_for_every_card(self):
        prompt = (
            "Show me the top 4 ergonomic mesh office chairs with adjustable lumbar "
            "support. A headrest would be nice but is not required. My budget is $150."
        )
        matching = _comparison(
            4,
            specs=["ergonomic", "mesh", "office", "adjustable lumbar support"],
        )
        self.assertIsNone(_product_comparison_acceptance_failure(prompt, matching))

        missing_on_one = _comparison(
            4,
            specs=["ergonomic", "mesh", "office", "adjustable lumbar support"],
        )
        missing_on_one["products"][2]["key_specs"] = [
            "mesh",
            "office",
            "adjustable lumbar support",
        ]
        failure = _product_comparison_acceptance_failure(prompt, missing_on_one)
        self.assertIsNotNone(failure)
        self.assertIn("ergonomic", failure)
        self.assertNotIn("headrest", failure.casefold())


if __name__ == "__main__":
    unittest.main()

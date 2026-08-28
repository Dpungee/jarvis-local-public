---
name: document-generation
description: Create polished PowerPoint decks, Word documents, Excel spreadsheets, and PDFs from a request. Use whenever the user wants a .pptx, .docx, .xlsx, or .pdf produced.
---

# Document generation

Create finished office documents with the deterministic `build_document` tool. Do not delegate ordinary document creation and do not write a Python generator script when this tool is available.

## Workflow

1. Infer sensible defaults from the request. Ask a question only when a truly necessary topic or target filename is missing.
2. Prepare the real content as bounded Markdown for Word, PDF, or PowerPoint. For an Excel workbook, use bounded JSON with `title`, the exact `sheet_name`, and `rows` so the requested table is structural rather than prose. Never use placeholder prose in a requested deliverable.
3. Call `build_document` exactly once with:
   - `path`: the exact requested workspace output path, including `.docx`, `.pdf`, `.xlsx`, or `.pptx`;
   - `document_type`: `docx`, `pdf`, `xlsx`, or `pptx`;
   - `content`: the complete Markdown source, or the complete JSON object for Excel.
4. Treat the returned artifact as created only when `verified` is true and the returned byte count is nonzero.
5. Report the exact workspace path. If the tool reports an error, state the error and do not claim success.

## Content guidance

### PowerPoint (.pptx)

Use one `##` section per slide, one idea per slide, and 3–6 concise bullets.

### Word (.docx) and PDF (.pdf)

Start with one `#` title, then use `##` section headings and short readable paragraphs.

### Excel (.xlsx)

Use JSON such as `{"title":"Metrics","sheet_name":"Metrics","rows":[["Check","Result"],["Build","Passed"]]}`. Keep headers explicit and cells free of executable formulas. The builder returns verified sheet names and table dimensions; compare those fields to the request before reporting success.

- Reports: lead with the answer or recommendation, include evidence and caveats, and finish with clear next steps.
- Never include credentials or secrets. The builder also redacts recognized secret forms and neutralizes spreadsheet formulas.

## Safety and verification

- Never claim a document was created until `build_document` returned verified metadata for the exact requested path.
- The tool creates only a new file inside the designated workspace and never overwrites an existing document.
- Path escapes, extension/type mismatches, and unsupported formats must fail closed.
- Never substitute `write_file` for a binary office document.

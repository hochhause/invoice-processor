# Confluence user guide — how to publish

`invoice-processor-user-guide.html` is written in **Confluence storage format** (Confluence's
native XHTML with `<ac:...>` macros: TOC, info/warning panels, status lozenges). It is not a
normal web page — open it in Confluence, not a browser.

## Paste into Confluence Cloud

1. Create a new page in the target space.
2. Type `/` and look for **"Insert Confluence storage format"** — if your editor doesn't offer
   it, use the **legacy editor** route: page → ⋯ → *Insert markup* → format **Confluence storage
   format**.
3. Paste the entire file content, insert, and review the rendering (TOC, status lozenges in the
   tables, info/warning panels).
4. Publish.

Alternative (admin/API): `PUT /wiki/api/v2/pages/{id}` with `body.representation = "storage"`
and the file content as `body.value`.

## Before publishing — check these

- **Tunnel URL** in section 2: `https://x3m2th39-8743.euw.devtunnels.ms` — verified live
  2026-06-11 (`devtunnel port list lyfegen-invoice-test --verbose` → `portForwardingUris`;
  stable while the persistent tunnel `lyfegen-invoice-test.euw` exists). If the tunnel is ever
  deleted and recreated, the subdomain changes — re-check and update the page.
- Contact person/email in section 9 still correct.

## Keeping it current

The page documents: workflow, the 6 job statuses, every "needs review" trigger, error table,
all settings fields, and the security section (tenant-gated tunnel, TLS, local data storage,
Anthropic API usage, Azure production outlook). If app behaviour changes in `app/main.py`,
`app/pipeline.py`, `app/llm.py`, `app/xml_export.py` or the settings UI, update the matching
section here and re-publish.

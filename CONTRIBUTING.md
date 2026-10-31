# Contributing

Thanks for helping. Bug reports from live use of the **experimental** modules
are especially valuable. Include the endpoint, the `LinkedIn-Version`, and the
LinkedIn error body, with ids and tokens redacted.

## Development

```bash
uv sync                      # Python 3.12+, installs dev tools
uv run pytest -q             # unit tests, all HTTP mocked with respx
uv run ruff check . && uv run ruff format --check .
```

## Rules of the road

- **Official, documented API only.** Build every request from
  [learn.microsoft.com/linkedin/marketing](https://learn.microsoft.com/linkedin/marketing)
  (or LinkedIn's official Postman collections) and cite the doc URL in the
  resource's docstring. No scraping or private endpoints.
- **API logic lives in the library.** The MCP layer (`linkedin_marketing/mcp`)
  only calls the library and returns `to_dict()` output.
- **Writes go through `Guard.execute`** with a `WritePlan`, so they get the dry
  run, read-only mode, allow-lists and the audit log. Don't add delete or billing
  operations.
- **Every endpoint needs a request-shaping test** that pins the method, path,
  query and body.
- **Errors** are `LinkedInError` subclasses that carry their values as
  attributes and `context`. Never put tokens or personal data in them.
- **Style:** guard clauses over nesting, named predicates for complex
  conditions, docstrings over multi-line comments. Ruff (with the complexity
  rules) must pass.
- Keep examples free of real people's and companies' data. Use placeholders like
  `123456789` and "Example Ltd".
- Use conventional commit messages (`feat:`, `fix:`, `docs:` ...).

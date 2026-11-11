# Writing a tenderkit plugin

A plugin is an installed Python distribution. Declare `tenderkit>=0.1,<0.2` in its dependencies and expose a zero-argument factory through an entry point. CLI and MCP discover plugins in their own Python environment. Install plugins you trust because entry points execute Python code.

## Notice sources

```toml
[project.entry-points."tenderkit.sources"]
example = "my_plugin:make_source"
```

```python
from tenderkit.model import Notice


class ExampleSource:
    name = "EXAMPLE"

    def fetch(self, start, end, query):
        yield Notice(
            self.name,
            "demo-1",
            title="Example software procurement",
            published=start,
            cpv=("72000000",),
            notice_type="ContractNotice",
        )


def make_source():
    return ExampleSource()
```

Implement `tenderkit.interfaces.Source`. Use a unique source name and stable notice numbers, and return that source name on every notice. Publication dates must fall within the inclusive window. A source may apply `SourceQuery.cpv_prefixes` and `SourceQuery.notice_types` to narrow the request. The profile applies its full filter locally. Raise an error for incomplete pagination so the refresh cannot commit partial results.

After installing the plugin, try `tenderkit fetch --source EXAMPLE --profile oprogramowanie` and inspect the saved notice with `tenderkit filter --source EXAMPLE --profile oprogramowanie`.

## Document platforms

```toml
[project.entry-points."tenderkit.platforms"]
example = "my_plugin:make_platform"
```

Implement `tenderkit.interfaces.Platform`:

| Method | Contract |
| --- | --- |
| `matches(url)` | Recognize the intended host and URL shape. Validate the parsed hostname, including redirect destinations during requests. |
| `procedure(notice)` | Return a `PlatformLink` with the procedure URL and stable key, or `None` when no procedure can be resolved. |
| `documents(link)` | Return `DocumentRef` objects. Each key must stay stable across visits, even when a download URL expires. |
| `download(ref, to)` | Write the selected document within the supplied directory and return its path. Leave registration and SHA-256 storage to tenderkit. |

Give the platform a unique `name`. For an inventory-only adapter, set `can_download = False` and implement `download` by raising `FetchError("LIST_ONLY")` from `tenderkit.platforms.backends`. Use authorized endpoints and respect the source's rate limits. Exercise empty and partial responses with fixtures and verify that a failed download leaves an actionable error.

The built-in adapters under `src/tenderkit/sources` and `src/tenderkit/platforms` show the protocols in use.

# Node 2 — Download Sources

## Type
HTTP fetch to local working directory

## Purpose
Retrieve the three discovered files to a temporary working directory. Raw source
files are never committed to the repository.

## Code
```python
def download(found):
    os.makedirs(_TMP, exist_ok=True)
    paths = {}
    for key, ext in (("d11", "xlsx"), ("d09", "xlsx"), ("reg", "ods")):
        url, _ = found[key]
        path = os.path.join(_TMP, f"s6_{key}.{ext}")
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        with open(path, "wb") as fh:
            fh.write(resp.content)
        paths[key] = path
    return paths
```

## Logic
1. Create `%TEMP%/s6_asylum` if absent.
2. Fetch each URL discovered by Node 1, raising on any non-200.
3. Write to a fixed local filename per source, so a re-run overwrites rather
   than accumulating quarterly copies.
4. Return the local paths.

## Query Parameters

| Parameter | Value |
|---|---|
| Working directory | `%TEMP%/s6_asylum` |
| `timeout` | 300 s (Asy_D09 is ~3.9 MB) |

## Behaviour
- Overwrites on re-run. No accumulation, no cache invalidation logic needed.
- Raw files stay outside the repository. `.xlsx` and `.ods` are never committed.
- Hard stop on any HTTP error — a partial download would fail parsing anyway,
  and failing here gives a clearer message.

## Connection
Input: discovered URLs from Node 1.
Output: `{d11, d09, reg} → local path` passed to Nodes 3, 4 and 7.

## Verified Output
- `s6_d11.xlsx` — 1,338,871 bytes
- `s6_d09.xlsx` — 3,930,149 bytes
- `s6_reg.ods` — 272,769 bytes
- Verified 2026-07-25 (initial build)

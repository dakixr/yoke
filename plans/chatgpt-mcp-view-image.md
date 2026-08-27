# ChatGPT MCP `view_image(path)`

## Goal

Add one ChatGPT-facing MCP tool:

```text
view_image(path: str)
```

The tool reads an image file from the Yoke MCP host and returns the original
bytes as native MCP `image` content. ChatGPT should receive an `ImageContent`
block with the concrete MIME type instead of JSON containing a data URL or a
text placeholder.

This is a host-to-ChatGPT feature. It is separate from
`plans/chatgpt-mcp-binary-file-transfer.md`, which covers moving binary bytes in
the opposite direction, from ChatGPT to the Yoke host.

## User problem

The current ChatGPT MCP server can read UTF-8 files, search, patch files, and run
commands, but it cannot show ChatGPT an image that already exists on the host.
`read_file` intentionally rejects binary data, and the MCP adapter currently
encodes every successful local tool result as JSON text plus
`structuredContent`.

That forces bad workarounds. A model can inspect metadata with shell commands,
but it cannot visually inspect the file through the connector. Returning base64
inside ordinary JSON would also be the wrong MCP representation because MCP
already has a native `ImageContent` type.

The target behavior is one call:

```text
view_image({"path":"artifacts/screenshot.png"})
```

and one successful MCP result containing the image itself.

## Scope

This plan adds only `view_image(path)` to `yoke-mcp`.

In scope:

- PNG, JPEG, GIF, and WebP input.
- Relative and absolute paths with the same path rules as Yoke's existing MCP
  file tools.
- Validation that the bytes are a supported image and can actually decode.
- Explicit compressed-file, decoded-pixel, and response-wire limits.
- Native MCP `ImageContent` on success.
- Normal Yoke MCP tool errors for missing, oversized, invalid, or unsupported
  files.
- Contract and real Streamable HTTP tests.
- MCP server documentation and exact-tool-list updates.

Out of scope:

- Changing `read_file` to return images.
- Image resizing, transcoding, recompression, thumbnails, or a `detail` option.
- Writing images or other binary files to the host.
- Passing through image content returned by downstream `mcp_call` servers.
- Adding `view_image` to the interactive Yoke agent or CLI tool set.
- Browser automation, ChatGPT conversation identity, or connector splitting.

## Current code to preserve

The implementation should fit the existing MCP-only path rather than create a
second server stack.

`src/yoke/mcp_server/registry.py` owns the explicit ChatGPT-visible allowlist.
Each `ExposedTool` points at a `LocalTool` class, carries annotations, and gets
its schema from Pydantic.

`src/yoke/mcp_server/adapter.py` binds the configured root, parses the Pydantic
arguments, runs the tool through `ProcessRuntime`, logs only the tool name,
duration, and success state, then converts the returned dictionary into an MCP
`CallToolResult`.

`src/yoke/agent/tools/base.py` already provides the path behavior needed here.
`WorkspaceTool._resolve_path()` resolves relative paths from the configured MCP
root and accepts normal absolute paths. `view_image` should use that method so
it neither widens nor narrows the filesystem contract compared with
`read_file`, `apply_patch`, `rg`, and `fd`.

Pillow is already a runtime dependency. `src/yoke/agent/image_data.py` also
contains image handling code, but its job is different: it may resize or
re-encode images for prompt-safe data URLs. `view_image` must preserve the file
bytes exactly, so it should not call `local_image_to_data_url()` or
`image_bytes_to_data_url()`.

The installed MCP SDK exposes `mcp.types.ImageContent` with this wire shape:

```text
type: "image"
data: base64 string
mimeType: string
```

That type should be used directly rather than building an untyped dictionary.

## External reference and the parts worth copying

The comparison with `chat-on-steroids` found a useful implementation detail:
its `view_image` does not treat a file signature as proof that an image is safe
to return. It bounds the encoded response, validates the format, and forces a
real pixel decode before an MCP image block is emitted. That matters because a
malformed image in native MCP content can fail at the consumer after the tool
has already claimed success.

Use the same safety model in Yoke, adapted to Pillow and Yoke's existing path
rules:

- 8 MiB maximum serialized image-result budget.
- 64 KiB reserved for JSON-RPC and result metadata.
- Raw image byte ceiling derived from base64's 4/3 expansion.
- 16 Mi decoded-pixel ceiling.
- 64 MiB estimated decoded-pixel byte ceiling.
- Full image decode before returning success.

The raw-byte ceiling from those numbers is:

```text
floor((8 MiB - 64 KiB) * 3 / 4) = 6,242,304 bytes
```

The limit is intentionally about the response, not Yoke's 4 MiB incoming MCP
request limit. `view_image` has a tiny request and a potentially large response.

## Public tool contract

Register this exact tool name:

```text
view_image
```

The input schema should contain exactly one field:

```text
path: str
```

`path` must be non-empty. A useful description is:

> Local path to a PNG, JPEG, GIF, or WebP image. Relative paths resolve from the configured Yoke MCP root; absolute paths are allowed.

The tool annotation is read-only:

```text
readOnlyHint: true
destructiveHint: false
idempotentHint: true
openWorldHint: false
```

Do not add a `detail`, `max_bytes`, MIME override, resize flag, or other model
input in the first version. The model should only decide which file to view.
Transport and decode limits remain server policy.

## Successful result contract

On success, return one MCP `ImageContent` item:

```text
CallToolResult(
    content=[
        ImageContent(
            type="image",
            data=<base64 of the exact file bytes>,
            mime_type=<detected MIME type>,
        )
    ],
    is_error=False,
)
```

Use the SDK's Python field spelling when constructing the model and let Pydantic
serialize it to the MCP `mimeType` wire key.

Do not include the base64 again in `structured_content`. Do not include a data
URL. Do not add a second text block containing the encoded bytes. The native
image block is the payload.

Preserving the original bytes is part of the contract. If the caller views a
valid PNG, the bytes obtained by base64-decoding `ImageContent.data` must equal
the bytes on disk at the time the file was read.

## Error contract

Failures should use the same recoverable MCP tool-error representation as the
rest of Yoke. The adapter can continue to return JSON text plus
`structuredContent` for errors.

Useful model-facing errors include:

- path does not exist,
- path is not a regular file,
- image exceeds the raw transport ceiling,
- unsupported format,
- invalid or truncated image data,
- decoded image exceeds the pixel or decoded-byte ceiling,
- file read fails because of normal OS permissions or I/O errors.

Error results must not include file bytes or base64 fragments.

Do not infer the MIME type from the filename extension. A file named `photo.png`
whose bytes are JPEG should be returned as `image/jpeg`. A file named
`photo.png` containing text should fail as invalid image data.

## Implementation design

### 1. Add the MCP-local image tool

Create `src/yoke/mcp_server/files.py` and define an MCP-specific
`WorkspaceTool`, for example `MCPViewImageTool`.

The Pydantic model should expose only:

```python
path: str = Field(min_length=1, ...)
```

Keep this class in `mcp_server` rather than `agent/tools`. Its reason for
existing is the ChatGPT MCP transport's native image result, and the interactive
agent already has separate multimodal image handling through `attach_image`.

`execute()` still needs to satisfy `LocalTool.execute() -> dict[str, object]` so
it can run through the existing `ProcessRuntime`. Return an internal dictionary
on success containing only the data needed by the MCP encoder, for example:

```text
ok
path
mime_type
data_base64
bytes
```

That dictionary is an internal handoff between the tool and the adapter. It is
not the final MCP representation.

Completion criterion: the class parses the one-field schema, uses
`WorkspaceTool._resolve_path()`, and can produce a validated internal image
result without changing the generic Yoke agent tools.

### 2. Read the file under a hard byte ceiling

Define module constants in `src/yoke/mcp_server/files.py`:

```text
MAX_VIEW_IMAGE_WIRE_BYTES = 8 * 1024 * 1024
MAX_VIEW_IMAGE_WIRE_OVERHEAD = 64 * 1024
MAX_VIEW_IMAGE_BYTES = floor((WIRE_BYTES - OVERHEAD) * 3 / 4)
MAX_VIEW_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_VIEW_IMAGE_DECODED_BYTES = 64 * 1024 * 1024
```

Check `path.is_file()` before reading. Use the file's stat size for the clear
common-case error, but do not rely on stat alone because the file can grow
between stat and read.

Open the file in binary mode and read at most `MAX_VIEW_IMAGE_BYTES + 1` bytes.
If the extra byte exists, reject the image. This closes the stat/read growth
race without allowing `Path.read_bytes()` to allocate an unbounded file.

Only base64-encode after all image validation succeeds.

Completion criterion: no code path can read or base64-encode more than the
configured raw-image ceiling.

### 3. Detect the format from decoded image data

Use Pillow as the decoder. Support exactly these initial formats:

```text
PNG  -> image/png
JPEG -> image/jpeg
GIF  -> image/gif
WEBP -> image/webp
```

The extension is not authoritative. Open the in-memory bytes with
`PIL.Image.open(BytesIO(data))` and derive the MIME type from `image.format`.
Reject any format outside the allowlist.

This map should live in the MCP image module. Do not change
`src/yoke/agent/image_data.py` merely to share four constants, because that
module has different transformation semantics and currently omits GIF from its
prompt-data path.

Completion criterion: supported images report their detected MIME type and a
renamed file cannot spoof the MIME value.

### 4. Prove the image decodes under bounded resource use

After `Image.open()` returns metadata, validate dimensions before calling
`load()`:

- width and height must both be positive,
- `width * height <= MAX_VIEW_IMAGE_PIXELS`,
- estimate decoded bytes using the number of image bands and reject values over
  `MAX_VIEW_IMAGE_DECODED_BYTES`.

Then call `image.load()` to force the decoder to consume the image data. Catch
Pillow decode errors and return a normal tool failure.

The point of `load()` is to reject files with a plausible header but a corrupt
compressed payload. Metadata-only detection is not enough for a native MCP
image result.

For GIF and animated WebP, the first implementation does not need animation
processing or frame extraction. The original file bytes are returned. Pillow
still needs to successfully decode the image before Yoke publishes it. If full
multi-frame validation proves necessary during implementation, keep that work
inside the same validator rather than changing the public tool schema.

Completion criterion: truncated or corrupt files cannot produce a successful
MCP image block, and a highly compressed image cannot expand past the explicit
decode limits.

### 5. Add a narrow native-result encoder to the MCP adapter

The current adapter assumes every local tool result should go through
`_encode_result()`, which serializes the complete dictionary into text and
`structured_content`. `view_image` needs one exception for successful native
image content.

Avoid a tool-name conditional such as `if name == "view_image"` in
`call_tool()`. Make the encoding choice part of the registry metadata instead.
A small change is enough:

```text
ExposedTool.result_kind = "json" | "image"
```

Default it to `"json"` so every existing tool keeps the current behavior.
Register `view_image` with `result_kind="image"`.

In `adapter.py`, replace the final unconditional `_encode_result(result)` with
an encoder that receives both the `ExposedTool` and the internal result.

Behavior:

1. If `result["ok"]` is false, always use the existing JSON error encoder.
2. If `result_kind == "json"`, use the existing encoder unchanged.
3. If `result_kind == "image"`, validate the internal result fields and build
   `CallToolResult(content=[ImageContent(...)], is_error=False)`.
4. Do not set `structured_content` on a successful image result.

Treat a malformed internal image dictionary as an adapter failure, not as a
partially successful image result. Log the server-side exception through the
existing final adapter boundary and send a short tool error without exposing
the malformed payload.

This keeps the change generic enough to avoid a hard-coded special case while
leaving every existing tool untouched.

Completion criterion: all ten current tools still encode exactly as before,
while only successful `view_image` calls use native image content.

### 6. Register `view_image`

Update `src/yoke/mcp_server/registry.py`:

- import `MCPViewImageTool`,
- add `view_image` immediately after `read_file`,
- use the existing `READ_ONLY` annotations,
- set `result_kind="image"`,
- keep the description focused on viewing an existing local image.

The externally visible order should become:

```text
read_file
view_image
rg
fd
skill
apply_patch
exec_command
exec_python
process_io
mcp_inspect
mcp_call
```

The connector grows from ten tools to eleven. There is no need for a second
connector because one small read-only schema does not justify another ChatGPT
app or OAuth connection.

Completion criterion: `tools/list` advertises exactly eleven tools and
`view_image` has only the `path` input property with read-only annotations.

## Test plan

### Contract tests

Update `tests/yoke/mcp_server/test_contract.py`.

Change `EXPECTED_TOOLS` to the eleven-tool list above and update the allowlist
assertion to account for the new MCP-local tool.

Add a schema/annotation assertion for `view_image`:

- input properties are exactly `path`,
- `path` is required,
- read-only hint is true,
- destructive hint is false,
- open-world hint is false.

Add a successful PNG test:

1. create a tiny valid PNG,
2. call `view_image`,
3. assert `is_error` is false,
4. assert there is one content item and it is `ImageContent`,
5. assert MIME is `image/png`,
6. base64-decode the returned data and compare it byte-for-byte with the file,
7. assert successful `structured_content` is absent.

Repeat format coverage for JPEG, GIF, and WebP. These can be parameterized.
The test only needs small images, and Pillow is already available in the test
environment.

### Path tests

Extend the existing path-semantics coverage with `view_image`:

- relative path under the configured root,
- `../` path resolving outside the root,
- ordinary absolute path.

The expected behavior should match Yoke's documented MCP path rules. This test
is important because `view_image` is not supposed to introduce an accidental
sandbox that differs from the rest of the server.

### Invalid-image tests

Cover these failures:

- missing path argument produces the existing recoverable validation error,
- missing file,
- directory instead of file,
- text bytes with a `.png` extension,
- a valid image renamed to a misleading extension still reports the MIME from
  the bytes,
- truncated PNG or JPEG data,
- unsupported image format such as BMP or TIFF.

Every failure must have `is_error=True` and no `ImageContent` item.

### Limit tests

Add focused tests for each independent bound:

- raw file larger than `MAX_VIEW_IMAGE_BYTES`,
- image whose declared pixel count exceeds `MAX_VIEW_IMAGE_PIXELS`,
- image whose estimated decoded bytes exceed
  `MAX_VIEW_IMAGE_DECODED_BYTES`.

Keep the tests cheap. Factor the validation helper so tests can pass smaller
limits rather than allocating a real 64 MiB decoded image. Production constants
remain fixed.

Add a direct assertion for the base64 budget formula:

```text
4 * ceil(MAX_VIEW_IMAGE_BYTES / 3) + MAX_VIEW_IMAGE_WIRE_OVERHEAD
    <= MAX_VIEW_IMAGE_WIRE_BYTES
```

This catches a later constant change that accidentally makes the native image
response larger than its stated budget.

### Adapter regression tests

The native result path must not change ordinary tools. Keep the current
`read_file` structured result assertion and add a focused adapter test proving:

- `read_file` still returns text plus `structured_content`,
- `view_image` success returns native image content without
  `structured_content`,
- `view_image` failure returns the normal structured JSON error.

### Real Streamable HTTP test

Add one test in `tests/yoke/mcp_server/test_transport.py` using the existing
`http_client` helper. Create a tiny PNG, call `view_image` over the real ASGI
Streamable HTTP stack, and verify the returned `ImageContent` bytes and MIME
type.

The in-memory contract test proves adapter behavior. This HTTP test proves the
native image survives MCP model serialization and the actual transport used by
ChatGPT.

There is no need to duplicate the image cases across legacy and 2026 protocol
eras. Protocol negotiation is independent of the tool payload, and the existing
transport suite already covers the server's dual-era behavior.

## Documentation changes

Update `src/yoke/docs/mcp-server.md`.

Change the stated tool count from ten to eleven and add `view_image` beside
`read_file` in the list. Document the behavior in one short paragraph:

- accepts a local PNG, JPEG, GIF, or WebP path,
- relative paths resolve from the configured root and absolute paths remain
  valid,
- returns native MCP image content,
- preserves the source bytes,
- rejects invalid and oversized images.

Do not add image-viewing rules to the server-level instruction string unless
real ChatGPT usage shows that the model fails to discover the tool from its
name and description. The first version should avoid permanent prompt text for
a behavior the tool schema already describes.

## Logging and privacy

Keep the current MCP logging rule. Log the tool name, duration, and success
state only.

Do not log:

- base64,
- image bytes,
- image metadata extracted by Pillow,
- file contents.

The adapter currently does not log result bodies, so no logging redesign is
needed.

The returned image is intentionally model-visible. That is the purpose of the
tool. Authentication, OS permissions, and Yoke's existing MCP path semantics
remain the access boundary.

## Implementation order

Implement in this order so each step has a checkable stopping point:

1. Add `MCPViewImageTool` and bounded image validation in
   `src/yoke/mcp_server/files.py`. Unit-test format detection, decode rejection,
   and limits before changing the public registry.
2. Add `result_kind` to `ExposedTool` and the native image success encoder in
   `adapter.py`. Prove existing JSON tools are unchanged.
3. Register `view_image` and update the exact tool-list contract test.
4. Add PNG/JPEG/GIF/WebP result tests plus path and invalid-image cases.
5. Add the real Streamable HTTP native-image test.
6. Update `src/yoke/docs/mcp-server.md`.
7. Run the focused MCP suite, then the normal repository quality gates required
   for an MCP server change.

## Acceptance criteria

The feature is complete when all of the following are true:

- `yoke-mcp` advertises exactly one new tool named `view_image`.
- Its public input schema contains only required `path: string`.
- It is annotated read-only and non-destructive.
- Relative and absolute paths behave like the existing Yoke MCP file tools.
- PNG, JPEG, GIF, and WebP are detected from their bytes, not their extensions.
- A successful call returns exactly one native MCP `ImageContent` block.
- Base64-decoding that block produces the exact file bytes read from disk.
- A successful image result does not duplicate the base64 in text or
  `structuredContent`.
- Corrupt, truncated, unsupported, oversized, or excessive-decoded-size images
  return normal recoverable MCP tool errors.
- No read path can load more than `MAX_VIEW_IMAGE_BYTES + 1` compressed bytes.
- The encoded image plus reserved metadata fits inside the configured 8 MiB
  result budget.
- The implementation forces a real image decode before publishing success.
- Existing MCP tools retain their current schemas, annotations, execution, and
  JSON result encoding.
- The native image result works through the real Streamable HTTP app used by
  ChatGPT.
- The MCP documentation lists eleven tools and describes `view_image`.

## Files expected to change

Implementation should normally be limited to:

```text
src/yoke/mcp_server/files.py                  new
src/yoke/mcp_server/registry.py
src/yoke/mcp_server/adapter.py
tests/yoke/mcp_server/test_contract.py
tests/yoke/mcp_server/test_transport.py
src/yoke/docs/mcp-server.md
```

If implementation needs broad changes outside that set, stop and re-check the
design first. `view_image(path)` should remain an MCP-local read-only feature,
not become a reason to restructure Yoke's agent image pipeline.

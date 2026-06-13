## `apply_patch`

Use the `apply_patch` tool to edit files.

The patch language is a stripped-down, file-oriented diff format:

*** Begin Patch
[ one or more file sections ]
*** End Patch

Every operation starts with one of these headers:

*** Add File: <path>
*** Delete File: <path>
*** Update File: <path>

An update may be immediately followed by:

*** Move to: <new path>

Update hunks start with `@@`, optionally followed by a class, function, or
other context header. Hunk lines must start with a space, `-`, or `+`.

Use enough unchanged context to identify each edit uniquely. Start with three
lines above and below a change. If that is insufficient, use one or more `@@`
headers to identify the containing class or function.

Grammar:

Patch := Begin { FileOp } End
Begin := "*** Begin Patch" NEWLINE
End := "*** End Patch" NEWLINE
FileOp := AddFile | DeleteFile | UpdateFile
AddFile := "*** Add File: " path NEWLINE { "+" line NEWLINE }
DeleteFile := "*** Delete File: " path NEWLINE
UpdateFile := "*** Update File: " path NEWLINE [ MoveTo ] { Hunk }
MoveTo := "*** Move to: " newPath NEWLINE
Hunk := "@@" [ header ] NEWLINE { HunkLine } [ "*** End of File" NEWLINE ]
HunkLine := (" " | "-" | "+") text NEWLINE

Example:

*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: src/app.py
*** Move to: src/main.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** Delete File: obsolete.txt
*** End Patch

Remember:

- Include an Add, Delete, or Update header for every operation.
- Prefix every added file-content line with `+`.
- Paths may be relative or absolute.

import assert from "node:assert/strict";
import test from "node:test";
import { Marked } from "../src/yoke/web/assets/vendor/marked.esm.js";
import { mathExtension } from "../src/yoke/web/assets/js/session/markdown/math.js";

const markdown = new Marked({ gfm: true, breaks: false }, mathExtension);
const plain = new Marked({ gfm: true, breaks: false });
function formulas(source) {
  const found = [];
  markdown.walkTokens(markdown.lexer(source), (token) => {
    if (token.type.startsWith("yokeMath") && token.text !== null) {
      found.push([token.text.trim(), token.display]);
    }
  });
  return found;
}

for (const [name, source, expected] of [
  ["single dollar inline", "The roots are $x=2$.", [["x=2", false]]],
  ["quadratic formula", String.raw`Roots: $x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}$.`, [[String.raw`x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}`, false]]],
  ["numeric math", "$2+2=4$ and $25$", [["2+2=4", false], ["25", false]]],
  ["multiple formulas", "$x$ and $y$.", [["x", false], ["y", false]]],
  ["markup characters", String.raw`$a*b*c < d_0 & \text{a_b}$`, [[String.raw`a*b*c < d_0 & \text{a_b}`, false]]],
  ["bold and italic", "**$x_i$** and *$y^2$*", [["x_i", false], ["y^2", false]]],
  ["lists", "- $x$\n- $y$", [["x", false], ["y", false]]],
  ["blockquote", "> $x^2$", [["x^2", false]]],
  ["table cells", "| x | y |\n| - | - |\n| $x$ | $y$ |", [["x", false], ["y", false]]],
  ["link label only", "[$x$](https://example.test/$y$)", [["x", false]]],
  ["currency mixed with math", "It costs $25 and $30, then $x$.", [["x", false]]],
  ["currency range mixed with math", "Between $5-$10, with $x$.", [["x", false]]],
  ["digit after invalid close", "$x$2 and $y$", [["y", false]]],
  ["escaped open", String.raw`\$x$ and $y$`, [["y", false]]],
  ["escaped close", String.raw`$x\$ and $y$`, [["y", false]]],
  ["escaped dollar inside TeX", String.raw`$\text{cost: \$25}$`, [[String.raw`\text{cost: \$25}`, false]]],
  ["even backslashes before opener", String.raw`\\$x$`, [["x", false]]],
  ["odd backslashes before opener", String.raw`\\\$x$`, []],
  ["backslash inline", String.raw`\(x^2\)`, [["x^2", false]]],
  ["backslash display", String.raw`\[x^2\]`, [["x^2", true]]],
  ["double dollar wins", "$$x^2$$ and $y$", [["x^2", true], ["y", false]]],
  ["multiline display", "Before\n$$\nx^2\n+ y^2\n$$\nAfter", [["x^2\n+ y^2", true]]],
  ["blockquote display", "> \\[\n> x^2\n> \\]", [["x^2", true]]],
  ["array line breaks", String.raw`\[\begin{matrix}a & b \\ c & d\end{matrix}\]`, [[String.raw`\begin{matrix}a & b \\ c & d\end{matrix}`, true]]],
  ["unmatched dollar before display", "$unclosed $$x$$", [["x", true]]],
  ["unmatched dollar before explicit inline", String.raw`$unclosed \(x\)`, [["x", false]]],
  ["unmatched dollar next paragraph", "$unclosed\n\n$y$", [["y", false]]],
  ["closing dollar inside code", "$unclosed `code$` and $y$", [["y", false]]],
  ["closing dollar inside HTML code", "$25 and <code>$x$</code>, then $y$", [["y", false]]],
  ["closing dollar inside link URL", "$25 and [price](https://example.test/$x$), then $y$", [["y", false]]],
  ["fenced open cannot steal prose", "```\n$unclosed\n```\n\n$x$", [["x", false]]],
]) {
  test(name, () => assert.deepEqual(formulas(source), expected));
}

for (const source of [
  "$25", "$ 25", "25$", "$1,200", "It costs $25 and $30", "between $5-$10",
  "$ x$", "$x $", "$ x $", "$x$2", "$x + y", "$x\ny$", "$x\\\ny$",
  String.raw`\$x\$`, String.raw`\$25 and \$30`, "$$unclosed", "$$$$",
  "`$x$`", "``$x$ and `x` ``", "```math\n$x$\n```", "~~~\n$$x$$\n~~~",
  "    $x$\n    $$y$$", "> ```\n> $x$\n> ```", "- `$x$`",
  "<code>$x$</code>", "<pre>$x$</pre>", '<span title="$x$">literal</span>',
  '[literal](https://example.test/$x$ "price $25")', "https://example.test/$x$",
  "&#36;x&#36;", "\uE0000\uE001", "ordinary **bold** and _italic_",
]) {
  test(`literal ${JSON.stringify(source)}`, () => {
    assert.deepEqual(formulas(source), []);
    assert.equal(markdown.parse(source), plain.parse(source));
  });
}

test("code containing explicit delimiters stays byte-for-byte literal", () => {
  for (const source of [String.raw`\(x\)`, String.raw`\[x\]`]) {
    for (const code of ["`" + source + "`", "```\n" + source + "\n```", "    " + source]) {
      assert.equal(markdown.parse(code), plain.parse(code));
    }
  }
});

test("math source is escaped before it reaches the HTML sanitizer", () => {
  const result = markdown.parse('$\\text{<img src=x onerror="alert(1)">}$');
  assert.ok(!result.includes("<img"));
  assert.ok(result.includes("&lt;img"));
  assert.ok(result.includes("&quot;"));
});

test("a math extension does not change other Marked instances", () => {
  assert.equal(plain.parse("$x$"), "<p>$x$</p>\n");
});

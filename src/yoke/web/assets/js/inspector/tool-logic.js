export function sortToolCallsChronologically(calls) {
  return [...(calls || [])].sort((left, right) => {
    const leftTime = Date.parse(left.time?.started || "") || 0;
    const rightTime = Date.parse(right.time?.started || "") || 0;
    if (leftTime !== rightTime) return leftTime - rightTime;
    return String(left.id || "").localeCompare(String(right.id || ""));
  });
}

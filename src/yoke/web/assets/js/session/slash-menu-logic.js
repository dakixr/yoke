export function slashMenuScrollDelta({ viewportTop, viewportBottom, itemTop, itemBottom }) {
  if (itemTop < viewportTop) return itemTop - viewportTop;
  if (itemBottom > viewportBottom) return itemBottom - viewportBottom;
  return 0;
}

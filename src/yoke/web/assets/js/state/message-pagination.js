const RECOVERABLE_CURSOR_CODES = new Set([
  "invalid_cursor",
  "invalid_cursor_anchor",
  "cursor_query_mismatch",
]);

const MAX_DUPLICATE_PAGES = 16;

export async function fetchOlderMessagePage({
  cursor,
  messages,
  fetchPage,
  fetchLatest,
}) {
  let currentCursor = cursor;
  let knownMessages = messages || [];
  let replacementMessages = null;
  let replacementSnapshotSeq = 0;
  let recoveredCursor = false;
  let duplicatePages = 0;

  while (currentCursor) {
    let response;
    try {
      response = await fetchPage(currentCursor);
    } catch (error) {
      if (!recoveredCursor && RECOVERABLE_CURSOR_CODES.has(error?.code)) {
        const fresh = await fetchLatest();
        replacementMessages = [...(fresh.data || [])].reverse();
        replacementSnapshotSeq = fresh.snapshotSeq || 0;
        knownMessages = replacementMessages;
        currentCursor = fresh.cursor?.next || null;
        recoveredCursor = true;
        continue;
      }
      throw error;
    }

    const nextCursor = response.cursor?.next || null;
    const knownIDs = new Set(knownMessages.map((message) => message?.id).filter(Boolean));
    const olderMessages = [...(response.data || [])]
      .reverse()
      .filter((message) => message?.id && !knownIDs.has(message.id));

    if (olderMessages.length || !nextCursor) {
      return {
        olderMessages,
        nextCursor,
        replacementMessages,
        replacementSnapshotSeq,
        recoveredCursor,
        duplicatePages,
      };
    }

    if (nextCursor === currentCursor || duplicatePages >= MAX_DUPLICATE_PAGES) {
      throw new Error("Older-message pagination did not advance.");
    }
    duplicatePages += 1;
    currentCursor = nextCursor;
  }

  return {
    olderMessages: [],
    nextCursor: null,
    replacementMessages,
    replacementSnapshotSeq,
    recoveredCursor,
    duplicatePages,
  };
}

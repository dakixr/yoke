import { useEffect, useRef, useState } from "../../vendor/htm-preact.js";

export const SIDEBAR_EDGE_OPEN_DELAY_MS = 120;
export const SIDEBAR_EDGE_CLOSE_DELAY_MS = 220;

export function useSidebarEdgeReveal(sidebarOpen) {
  const [peeking, setPeeking] = useState(false);
  const openTimer = useRef(null);
  const closeTimer = useRef(null);

  const clearOpenTimer = () => {
    if (openTimer.current == null) return;
    clearTimeout(openTimer.current);
    openTimer.current = null;
  };
  const clearCloseTimer = () => {
    if (closeTimer.current == null) return;
    clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };

  useEffect(() => {
    if (sidebarOpen) setPeeking(false);
    clearOpenTimer();
    clearCloseTimer();
    return () => {
      clearOpenTimer();
      clearCloseTimer();
    };
  }, [sidebarOpen]);

  const beginEdgeReveal = () => {
    if (sidebarOpen || peeking || openTimer.current != null) return;
    clearCloseTimer();
    openTimer.current = setTimeout(() => {
      openTimer.current = null;
      setPeeking(true);
    }, SIDEBAR_EDGE_OPEN_DELAY_MS);
  };

  const cancelEdgeReveal = () => {
    clearOpenTimer();
  };

  const holdSidebar = () => {
    if (!peeking) return;
    clearCloseTimer();
  };

  const releaseSidebar = () => {
    if (!peeking || sidebarOpen) return;
    clearCloseTimer();
    closeTimer.current = setTimeout(() => {
      closeTimer.current = null;
      setPeeking(false);
    }, SIDEBAR_EDGE_CLOSE_DELAY_MS);
  };

  const dismissSidebar = () => {
    clearOpenTimer();
    clearCloseTimer();
    setPeeking(false);
  };

  return {
    peeking: peeking && !sidebarOpen,
    beginEdgeReveal,
    cancelEdgeReveal,
    holdSidebar,
    releaseSidebar,
    dismissSidebar,
  };
}

import { useEffect, useRef, useState } from "../../vendor/htm-preact.js";
import { store } from "./store.js";

export function useStore(selector) {
  const selectorRef = useRef(selector);
  selectorRef.current = selector;
  const value = selector(store.getState());
  const valueRef = useRef(value);
  valueRef.current = value;
  const [, rerender] = useState(0);
  useEffect(() => {
    const update = () => {
      const next = selectorRef.current(store.getState());
      if (!Object.is(next, valueRef.current)) {
        valueRef.current = next;
        rerender((version) => version + 1);
      }
    };
    const unsubscribe = store.subscribe(update);
    update();
    return unsubscribe;
  }, []);
  return value;
}

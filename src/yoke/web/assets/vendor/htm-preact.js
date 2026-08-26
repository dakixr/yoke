import { h, render, Fragment, createContext } from "preact";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "preact/hooks";
import htm from "./htm.module.js";

const html = htm.bind(h);

export {
  Fragment,
  createContext,
  h,
  html,
  render,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
};

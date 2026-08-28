export function chatActivityForRuntime(runtime) {
  if (!runtime) return null;
  if (runtime.state === "running") return runtime.activity || "Working";
  if (runtime.state === "stopping") return runtime.activity || "Stopping";
  return null;
}

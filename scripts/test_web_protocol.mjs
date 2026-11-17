import assert from "node:assert/strict";
import { YokeApi } from "../src/yoke/web/assets/js/api/client.js";

const required = ["globalEvents", "promptAdmission", "inputLifecycle", "steering", "queueEditor"];
const capabilities = { protocolVersion: "3", features: Object.fromEntries(required.map((name) => [name, true])) };
const api = new YokeApi();
let calls = 0;
let data = capabilities;
api.request = async () => { calls++; return { data }; };
assert.deepEqual(await api.capabilities(), { data: capabilities });
for (const feature of required) {
  for (const value of [false, undefined]) {
    data = { ...capabilities, features: { ...capabilities.features, [feature]: value } };
    const before = calls;
    await assert.rejects(api.capabilities(), new RegExp(`${feature}.*Upgrade.*restart.*reload`));
    assert.equal(calls, before + 1, "incompatible daemons must not be retried");
  }
}
for (const protocolVersion of ["2", undefined]) {
  data = { ...capabilities, protocolVersion };
  await assert.rejects(api.capabilities(), /protocol 3.*Upgrade.*restart.*reload/);
}
console.log("Protocol 3 capability validation passed");

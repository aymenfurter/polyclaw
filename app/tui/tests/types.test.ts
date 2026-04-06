/**
 * Tests for config types -- compile-time validation.
 *
 * These tests verify that the type definitions are consistent and
 * that objects conforming to the interfaces can be created without
 * errors.
 */

import { describe, test, expect } from "bun:test";
import type {
  StatusResponse,
  DeployResult,
  LogStream,
  SlashCommand,
  ModelEntry,
  SessionPickerEntry,
  AcaConfig,
} from "../src/config/types.js";

describe("Config types (compile-time validation)", () => {
  test("StatusResponse can be constructed", () => {
    const status: StatusResponse = {
      azure: { logged_in: true, subscription: "sub-1" },
      tunnel: { active: true, url: "https://tunnel.example.com" },
    };
    expect(status.azure?.logged_in).toBe(true);
  });

  test("DeployResult can be constructed", () => {
    const result: DeployResult = {
      baseUrl: "http://localhost:8080",
      instanceId: "abc123",
      reconnected: false,
    };
    expect(result.baseUrl).toBe("http://localhost:8080");
  });

  test("LogStream has a stop function", () => {
    const stream: LogStream = {
      stop: () => {},
    };
    expect(typeof stream.stop).toBe("function");
  });

  test("SlashCommand shape", () => {
    const cmd: SlashCommand = { cmd: "/test", desc: "A test command" };
    expect(cmd.cmd.startsWith("/")).toBe(true);
  });

  test("ModelEntry shape", () => {
    const model: ModelEntry = { id: "gpt-4o", name: "GPT-4o" };
    expect(model.id).toBe("gpt-4o");
  });

  test("SessionPickerEntry shape", () => {
    const session: SessionPickerEntry = {
      id: "s1",
      label: "My session",
      detail: "2024-01-01",
    };
    expect(session.id).toBe("s1");
  });

  test("AcaConfig shape", () => {
    const cfg: AcaConfig = {
      deployId: "d1",
      deployTag: "v1",
      resourceGroup: "rg-test",
      location: "eastus",
      acrName: "myacr",
      acrLoginServer: "myacr.azurecr.io",
      environmentName: "staging",
      appName: "my-app",
      fqdn: "my-app.region.azurecontainerapps.io",
      storageAccountName: "storage1",
      storageShareName: "share1",
      vnetName: "vnet1",
      subnetName: "subnet1",
      adminPort: 8080,
      botPort: 3978,
      adminSecret: "secret",
      lastDeployed: "2024-01-01",
    };
    expect(cfg.resourceGroup).toBe("rg-test");
  });
});

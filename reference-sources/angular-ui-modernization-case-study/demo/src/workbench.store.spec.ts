import { TestBed } from "@angular/core/testing";
import { describe, expect, it, vi } from "vitest";
import { CASES, CaseRow } from "./domain";
import { CASE_API, WorkbenchStore } from "./workbench.store";
import { DRAFT_KEY, DRAFT_STORAGE, DraftStorage } from "./draft-storage";

function setup(load = vi.fn().mockResolvedValue(CASES)) {
  TestBed.configureTestingModule({
    providers: [WorkbenchStore, { provide: CASE_API, useValue: { load } }],
  });
  return { store: TestBed.inject(WorkbenchStore), load };
}

describe("Feature-owned state", () => {
  it("keeps filtered-out selections and exposes their count", async () => {
    const { store } = setup();
    await store.reload();
    store.toggle("CASE-101");
    store.filter({ status: "Review" });
    expect(store.selected().has("CASE-101")).toBe(true);
    expect(store.hiddenSelected()).toBe(1);
    store.filter({ status: "all" });
    expect(store.hiddenSelected()).toBe(0);
  });
  it("preserves data and selection on failure, then recovers", async () => {
    const { store, load } = setup();
    await store.reload();
    store.toggle("CASE-101");
    load.mockRejectedValueOnce(new Error("outage"));
    await store.reload(true);
    expect(store.error()).toContain("preserved");
    expect(store.rows()).toEqual(CASES);
    expect(store.selected().has("CASE-101")).toBe(true);
    await store.reload();
    expect(store.error()).toBe("");
    expect(store.loading()).toBe(false);
  });
  it("does not overwrite a newer request with an old completion", async () => {
    let resolveOld!: (rows: readonly CaseRow[]) => void;
    const old = new Promise<readonly CaseRow[]>((resolve) => {
      resolveOld = resolve;
    });
    const { store } = setup(
      vi.fn().mockReturnValueOnce(old).mockResolvedValueOnce([CASES[0]]),
    );
    const first = store.reload();
    await store.reload();
    resolveOld(CASES);
    await first;
    expect(store.rows()).toEqual([CASES[0]]);
  });
  it("ignores a pending response after the owning injector is destroyed", async () => {
    let resolve!: (rows: readonly CaseRow[]) => void;
    const { store } = setup(
      vi.fn().mockReturnValue(
        new Promise<readonly CaseRow[]>((r) => {
          resolve = r;
        }),
      ),
    );
    const pending = store.reload();
    TestBed.resetTestingModule();
    resolve(CASES);
    await pending;
    expect(store.rows()).toEqual([]);
  });
  it("toggles sort direction through a UI-neutral intent", () => {
    const { store } = setup();
    store.sortBy("days");
    expect(store.query().direction).toBe("asc");
    store.sortBy("days");
    expect(store.query().direction).toBe("desc");
  });
});

describe("Draft storage adapter", () => {
  it("restores versioned data and writes only under its own key", () => {
    const draft = {
      version: 1 as const,
      owner: "Alex",
      priority: "High" as const,
      note: "Check boundaries",
    };
    const getItem = vi.fn().mockReturnValue(JSON.stringify(draft));
    const setItem = vi.fn();
    TestBed.configureTestingModule({
      providers: [{ provide: DRAFT_STORAGE, useValue: { getItem, setItem } }],
    });
    const adapter = TestBed.inject(DraftStorage);
    expect(adapter.read()).toEqual(draft);
    adapter.save(draft);
    expect(setItem).toHaveBeenCalledWith(DRAFT_KEY, JSON.stringify(draft));
  });
  it("does not hide storage failures from the feature", () => {
    TestBed.configureTestingModule({
      providers: [
        {
          provide: DRAFT_STORAGE,
          useValue: {
            getItem: () => {
              throw new Error("blocked");
            },
          },
        },
      ],
    });
    expect(() => TestBed.inject(DraftStorage).read()).toThrow("blocked");
  });
});

import { inject, Injectable, InjectionToken } from "@angular/core";
import { Draft, parseDraft } from "./domain";

export const DRAFT_KEY = "angular-workbench:draft:v1";
export const DRAFT_STORAGE = new InjectionToken<
  Pick<Storage, "getItem" | "setItem">
>("DRAFT_STORAGE", {
  factory: () => ({
    getItem: (key) => localStorage.getItem(key),
    setItem: (key, value) => localStorage.setItem(key, value),
  }),
});
@Injectable({ providedIn: "root" })
export class DraftStorage {
  private readonly storage = inject(DRAFT_STORAGE);
  read(): Draft | null {
    const raw = this.storage.getItem(DRAFT_KEY);
    return raw === null ? null : parseDraft(raw);
  }
  save(draft: Draft): void {
    this.storage.setItem(DRAFT_KEY, JSON.stringify(draft));
  }
}

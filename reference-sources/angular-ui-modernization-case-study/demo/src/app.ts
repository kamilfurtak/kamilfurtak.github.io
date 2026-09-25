import { Component, inject, signal } from "@angular/core";
import { FormBuilder, ReactiveFormsModule, Validators } from "@angular/forms";
import { Draft } from "./domain";
import { DraftStorage } from "./draft-storage";
import { NativeGridComponent } from "./grid-adapters";
import { PrimeGridComponent } from "./prime-grid";
import { WorkbenchStore } from "./workbench.store";

@Component({
  selector: "app-root",
  imports: [ReactiveFormsModule, NativeGridComponent, PrimeGridComponent],
  providers: [WorkbenchStore],
  templateUrl: "./app.html",
})
export class App {
  readonly store = inject(WorkbenchStore);
  private readonly drafts = inject(DraftStorage);
  readonly adapter = signal<"native" | "prime">("native");
  readonly message = signal("");
  readonly draftError = signal("");
  readonly form = inject(FormBuilder).nonNullable.group({
    owner: [
      "",
      [Validators.required, Validators.pattern(/\S/), Validators.maxLength(80)],
    ],
    priority: ["Normal" as Draft["priority"], Validators.required],
    note: [
      "",
      [
        Validators.required,
        Validators.pattern(/\S/),
        Validators.maxLength(500),
      ],
    ],
  });
  constructor() {
    void this.store.reload();
    try {
      const draft = this.drafts.read();
      if (draft) {
        this.form.patchValue(draft);
        this.message.set("Draft restored from this browser.");
      }
    } catch {
      this.draftError.set(
        "Saved draft is unavailable or invalid. You can continue in memory; the stored value is replaced only when you save.",
      );
    }
  }
  filterStatus(status: string): void {
    if (
      status === "all" ||
      status === "New" ||
      status === "Review" ||
      status === "Ready"
    )
      this.store.filter({ status });
  }
  saveDraft(): void {
    this.form.markAllAsTouched();
    this.message.set("");
    if (this.form.invalid) return;
    try {
      this.drafts.save({ version: 1, ...this.form.getRawValue() });
      this.draftError.set("");
      this.message.set(
        "Draft saved in this browser. Nothing was sent to a server.",
      );
    } catch {
      this.draftError.set(
        "Browser storage is unavailable. Your draft remains on screen; copy it before leaving.",
      );
    }
  }
}

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("./");
  await expect(
    page.getByRole("status").filter({ hasText: "6 cases shown" }),
  ).toBeVisible();
});

test("renderer replacement preserves sorting, filtering, selection and draft", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.getByRole("checkbox", { name: "Select CASE-103" }).check();
  await page.getByRole("button", { name: "Age in days", exact: true }).click();
  await page.getByRole("button", { name: "Age in days", exact: true }).click();
  await page.getByRole("searchbox", { name: "Search cases" }).fill("map");
  await page.getByRole("textbox", { name: "Draft owner" }).fill("Alex");
  await page
    .getByRole("textbox", { name: "Note", exact: true })
    .fill("Inspect boundary geometry");
  for (const renderer of ["PrimeNG table", "Native table"]) {
    await page.getByRole("button", { name: renderer, exact: true }).click();
    await expect(
      page.getByRole("checkbox", { name: "Select CASE-103" }),
    ).toBeChecked();
    await expect(
      page.getByRole("table").locator("tbody tr").first(),
    ).toContainText("CASE-103");
    await expect(
      page.getByRole("textbox", { name: "Note", exact: true }),
    ).toHaveValue("Inspect boundary geometry");
    await expect(page.getByRole("searchbox")).toHaveValue("map");
  }
  await page.getByRole("button", { name: "CASE-103", exact: true }).click();
  await expect(page.getByRole("complementary")).toContainText(
    "Map attachment check",
  );
  expect(errors).toEqual([]);
});

test("failed request preserves rows and recovers through retry", async ({
  page,
}) => {
  await page.getByRole("checkbox", { name: "Select CASE-101" }).check();
  await page.getByRole("button", { name: "Simulate failure" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Simulated request failed",
  );
  await expect(
    page.getByRole("checkbox", { name: "Select CASE-101" }),
  ).toBeChecked();
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: "Select CASE-101" }),
  ).toBeChecked();
});

test("validates and restores a local draft after reload", async ({ page }) => {
  await page.getByRole("button", { name: "Save local draft" }).click();
  await expect(
    page.getByText("Enter a name (up to 80 characters).", { exact: true }),
  ).toBeVisible();
  await page.getByRole("textbox", { name: "Draft owner" }).fill("Alex");
  await page
    .getByRole("textbox", { name: "Note", exact: true })
    .fill("Review before exporting");
  await page.getByRole("button", { name: "Save local draft" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Draft saved" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("textbox", { name: "Note", exact: true }),
  ).toHaveValue("Review before exporting");
  await expect(
    page.getByRole("status").filter({ hasText: "Draft restored" }),
  ).toBeVisible();
});

test("malformed and blocked storage leave the form usable", async ({
  page,
}) => {
  await page.evaluate(() =>
    localStorage.setItem("angular-workbench:draft:v1", "{broken"),
  );
  await page.reload();
  await expect(page.getByRole("alert")).toContainText(
    "Saved draft is unavailable or invalid",
  );
  await page.getByRole("textbox", { name: "Draft owner" }).fill("Alex");
  await page
    .getByRole("textbox", { name: "Note", exact: true })
    .fill("Keep in memory");
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new DOMException("Blocked", "SecurityError");
    };
  });
  await page.getByRole("button", { name: "Save local draft" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Browser storage is unavailable",
  );
  await expect(
    page.getByRole("textbox", { name: "Note", exact: true }),
  ).toHaveValue("Keep in memory");
});

test("keyboard and narrow viewport remain usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const native = page.getByRole("button", {
    name: "Native table",
    exact: true,
  });
  await native.focus();
  await page.keyboard.press("Enter");
  await expect(native).toHaveAttribute("aria-pressed", "true");
  const search = page.getByRole("searchbox");
  await search.focus();
  await page.keyboard.type("nothing matches");
  await expect(page.getByRole("table")).toContainText("No cases match");
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
});

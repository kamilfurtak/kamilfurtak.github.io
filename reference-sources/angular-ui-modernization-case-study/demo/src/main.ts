import { bootstrapApplication } from "@angular/platform-browser";
import { providePrimeNG } from "primeng/config";
import { App } from "./app";
bootstrapApplication(App, {
  providers: [providePrimeNG({ unstyled: true })],
}).catch((error) => console.error(error));

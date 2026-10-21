# Studio

This project was generated using [Angular CLI](https://github.com/angular/angular-cli) version 22.2.3.

## Development server

To start a local development server, run:

```bash
ng serve
```

Once the server is running, open your browser and navigate to `http://localhost:4200/`. The application will automatically reload whenever you modify any of the source files.

## Code scaffolding

Angular CLI includes powerful code scaffolding tools. To generate a new component, run:

```bash
ng generate component component-name
```

For a complete list of available schematics (such as `components`, `directives`, or `pipes`), run:

```bash
ng generate --help
```

## Building

To build the project run:

```bash
ng build
```

This will compile your project and store the build artifacts in the `dist/` directory. By default, the production build optimizes your application for performance and speed.

## Running unit tests

To execute unit tests with the [Vitest](https://vitest.dev/) test runner, use the following command:

```bash
ng test
```

## Running end-to-end tests

For end-to-end (e2e) testing, run:

```bash
ng e2e
```

Angular CLI does not come with an end-to-end testing framework by default. You can choose one that suits your needs.

## Additional Resources

For more information on using the Angular CLI, including detailed command references, visit the [Angular CLI Overview and Command Reference](https://angular.dev/tools/cli) page.

## API connection

The Studio talks to `forge-lens serve` (default `http://localhost:7777`).
Use the ⚙ settings control in the header (or `localStorage` keys
`lens-api-base` / `lens-api-key`) to point at a different base URL or to
set the `X-Forge-Lens-Key` header when the server runs `auth_mode` of
`key`/`keys`. During development, `ng serve --proxy-config proxy.conf.json`
forwards `/v1` to `:7777` so no CORS setup is needed.

Note: the API key is stored in `localStorage`, which is readable by any
script running in the page — acceptable for a local-first tool bound to
loopback, but do not reuse a high-value key here if Studio is ever
served from a shared origin.

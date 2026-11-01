import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "./styles/tokens.css";
import "./styles/app.css";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { ApiError } from "./api/client";
import { keys } from "./api/hooks";
import { App } from "./App";
import { ToastProvider } from "./components/ui";

// A request answered 401 means the session ended: re-check the session (once; never in answer to
// the session check itself), and the shell sends you to sign in.
const sessionEnded = (error: unknown) => error instanceof ApiError && error.status === 401;
const recheckSession = () => void client.invalidateQueries({ queryKey: keys.session, refetchType: "active" });

const client = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      if (sessionEnded(error) && query.queryKey[0] !== keys.session[0]) recheckSession();
    },
  }),
  mutationCache: new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      if (sessionEnded(error) && mutation.options.mutationKey?.[0] !== "sign-in") recheckSession();
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 2_000,
      retry: (failures, error) => !(error instanceof ApiError && error.status < 500 && error.status > 0) && failures < 2,
      refetchOnWindowFocus: true,
    },
  },
});

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <BrowserRouter>
          <ToastProvider>
            <App />
          </ToastProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}

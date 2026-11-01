/** Theme preference: follow the system, or force light or dark (stored in this browser only). */

import { useCallback, useState } from "react";

export type ThemeChoice = "system" | "light" | "dark";
const KEY = "hollis-theme";

function read(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(choice: ThemeChoice) {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    // storage unavailable: the choice lasts for this page
  }
}

export function useTheme(): [ThemeChoice, () => void] {
  const [choice, setChoice] = useState<ThemeChoice>(read);
  const cycle = useCallback(() => {
    setChoice((current) => {
      const next: ThemeChoice = current === "system" ? "light" : current === "light" ? "dark" : "system";
      applyTheme(next);
      return next;
    });
  }, []);
  return [choice, cycle];
}

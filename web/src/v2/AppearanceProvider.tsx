import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  DEFAULT_APPEARANCE,
  applyAppearance,
  loadAppearanceCookie,
  loadAppearance,
  saveAppearance,
  serializeAppearanceCookie,
  type AppearancePreferences,
  type ResolvedMotion,
  type ResolvedTheme,
} from "./appearance";

type AppearanceContextValue = {
  preferences: AppearancePreferences;
  resolvedTheme: ResolvedTheme;
  resolvedMotion: ResolvedMotion;
  systemAccentAvailable: boolean;
  updateAppearance: (next: AppearancePreferences) => void;
  patchAppearance: (next: Partial<AppearancePreferences>) => void;
};

const AppearanceContext = createContext<AppearanceContextValue | null>(null);

type TauriTheme = "light" | "dark" | null;

type TauriWindowApi = {
  getCurrentWindow?: () => { setTheme?: (theme: TauriTheme) => Promise<void> };
};

declare global {
  interface Window {
    __TAURI__?: { window?: TauriWindowApi; core?: { invoke: <T>(command: string, args?: Record<string, unknown>) => Promise<T> } };
  }
}

function mediaQuery(query: string): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(query) : null;
}

function nativeChromeTheme(theme: ResolvedTheme): TauriTheme {
  return theme === "dark" || theme === "solarized-dark" ? "dark" : "light";
}

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [preferences, setPreferences] = useState<AppearancePreferences>(() => (
    typeof window === "undefined"
      ? DEFAULT_APPEARANCE
      : loadAppearanceCookie(document.cookie) ?? loadAppearance(window.localStorage)
  ));
  const [prefersDark, setPrefersDark] = useState(() => mediaQuery("(prefers-color-scheme: dark)")?.matches ?? true);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(() => mediaQuery("(prefers-reduced-motion: reduce)")?.matches ?? false);
  const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>("dark");
  const [resolvedMotion, setResolvedMotion] = useState<ResolvedMotion>("full");

  useEffect(() => {
    const colorScheme = mediaQuery("(prefers-color-scheme: dark)");
    const reducedMotion = mediaQuery("(prefers-reduced-motion: reduce)");
    const updateColorScheme = (event: MediaQueryListEvent) => setPrefersDark(event.matches);
    const updateReducedMotion = (event: MediaQueryListEvent) => setPrefersReducedMotion(event.matches);
    colorScheme?.addEventListener("change", updateColorScheme);
    reducedMotion?.addEventListener("change", updateReducedMotion);
    return () => {
      colorScheme?.removeEventListener("change", updateColorScheme);
      reducedMotion?.removeEventListener("change", updateReducedMotion);
    };
  }, []);

  useEffect(() => {
    const applied = applyAppearance(document.documentElement, preferences, {
      prefersDark,
      prefersReducedMotion,
      systemAccentAvailable: false,
    });
    saveAppearance(window.localStorage, preferences);
    document.cookie = serializeAppearanceCookie(preferences);
    setResolvedTheme(applied.theme);
    setResolvedMotion(applied.motion);

    const tauriWindow = window.__TAURI__?.window?.getCurrentWindow?.();
    if (tauriWindow?.setTheme) {
      void tauriWindow.setTheme(nativeChromeTheme(applied.theme)).catch(() => {
        document.documentElement.dataset.nativeThemeSync = "unavailable";
      });
      document.documentElement.dataset.nativeThemeSync = "requested";
    } else {
      document.documentElement.dataset.nativeThemeSync = "browser";
    }
  }, [preferences, prefersDark, prefersReducedMotion]);

  const value = useMemo<AppearanceContextValue>(() => ({
    preferences,
    resolvedTheme,
    resolvedMotion,
    systemAccentAvailable: false,
    updateAppearance: setPreferences,
    patchAppearance: (next) => setPreferences((current) => ({ ...current, ...next })),
  }), [preferences, resolvedMotion, resolvedTheme]);

  return <AppearanceContext.Provider value={value}>{children}</AppearanceContext.Provider>;
}

export function useAppearance(): AppearanceContextValue {
  const value = useContext(AppearanceContext);
  if (!value) throw new Error("useAppearance must be used within AppearanceProvider.");
  return value;
}

/**
 * Presentation views can also be rendered in isolation by the existing
 * component harness.  Keep those views deterministic while preserving the
 * strict hook for stateful settings controls.
 */
export function useOptionalAppearance(): AppearanceContextValue {
  return useContext(AppearanceContext) ?? {
    preferences: DEFAULT_APPEARANCE,
    resolvedTheme: "dark",
    resolvedMotion: "full",
    systemAccentAvailable: false,
    updateAppearance: () => undefined,
    patchAppearance: () => undefined,
  };
}

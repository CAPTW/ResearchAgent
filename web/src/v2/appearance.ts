export const THEMES = ["system", "light", "dark", "solarized-light", "solarized-dark"] as const;
export const ACCENTS = ["blue", "cyan", "violet", "green", "amber", "system"] as const;
export const DENSITIES = ["comfortable", "compact"] as const;
export const MOTIONS = ["system", "reduced", "full"] as const;
export const TIME_FORMATS = ["locale", "12h", "24h"] as const;

export type ThemePreference = (typeof THEMES)[number];
export type ResolvedTheme = Exclude<ThemePreference, "system">;
export type AccentPreference = (typeof ACCENTS)[number];
export type DensityPreference = (typeof DENSITIES)[number];
export type MotionPreference = (typeof MOTIONS)[number];
export type ResolvedMotion = Exclude<MotionPreference, "system">;
export type TimeFormatPreference = (typeof TIME_FORMATS)[number];

export type AppearancePreferences = {
  theme: ThemePreference;
  accent: AccentPreference;
  density: DensityPreference;
  motion: MotionPreference;
  /** Optional so pre-R2C stored preferences remain valid and resolve to locale. */
  timeFormat?: TimeFormatPreference;
};

export type SystemAppearance = {
  prefersDark: boolean;
  prefersReducedMotion: boolean;
  systemAccentAvailable?: boolean;
};

export const APPEARANCE_STORAGE_KEY = "xrml.appearance.v1";
export const APPEARANCE_COOKIE_KEY = "xrml_appearance_v1";
const APPEARANCE_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 365;
export const DEFAULT_APPEARANCE: AppearancePreferences = {
  theme: "system",
  accent: "blue",
  density: "comfortable",
  motion: "system",
  timeFormat: "locale",
};

function isOneOf<T extends string>(value: unknown, values: readonly T[]): value is T {
  return typeof value === "string" && values.includes(value as T);
}

export function isAppearancePreferences(value: unknown): value is AppearancePreferences {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AppearancePreferences>;
  return isOneOf(candidate.theme, THEMES)
    && isOneOf(candidate.accent, ACCENTS)
    && isOneOf(candidate.density, DENSITIES)
    && isOneOf(candidate.motion, MOTIONS)
    && (candidate.timeFormat === undefined || isOneOf(candidate.timeFormat, TIME_FORMATS));
}

export function resolveTimeFormat(value?: TimeFormatPreference): TimeFormatPreference {
  return value && isOneOf(value, TIME_FORMATS) ? value : "locale";
}

export function formatTimestamp(
  value: string | Date,
  preference: TimeFormatPreference = "locale",
  locale?: string,
): string {
  const parsed = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(parsed.getTime())) return typeof value === "string" ? value : "미확인";
  const mode = resolveTimeFormat(preference);
  return parsed.toLocaleString(locale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: mode === "12h" ? true : mode === "24h" ? false : undefined,
  });
}

export function resolveTheme(theme: ThemePreference, prefersDark: boolean): ResolvedTheme {
  return theme === "system" ? (prefersDark ? "dark" : "light") : theme;
}

export function resolveMotion(motion: MotionPreference, prefersReducedMotion: boolean): ResolvedMotion {
  return motion === "system" ? (prefersReducedMotion ? "reduced" : "full") : motion;
}

export function loadAppearance(storage: Pick<Storage, "getItem">): AppearancePreferences {
  const stored = storage.getItem(APPEARANCE_STORAGE_KEY);
  if (!stored) return DEFAULT_APPEARANCE;
  try {
    const parsed: unknown = JSON.parse(stored);
    return isAppearancePreferences(parsed) ? parsed : DEFAULT_APPEARANCE;
  } catch {
    return DEFAULT_APPEARANCE;
  }
}

export function saveAppearance(
  storage: Pick<Storage, "setItem">,
  preferences: AppearancePreferences,
): void {
  if (!isAppearancePreferences(preferences)) {
    throw new TypeError("Unsupported XRML appearance preference.");
  }
  storage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(preferences));
}

export function loadAppearanceCookie(cookieHeader: string): AppearancePreferences | null {
  const prefix = `${APPEARANCE_COOKIE_KEY}=`;
  const stored = cookieHeader
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  if (!stored) return null;
  try {
    const parsed: unknown = JSON.parse(decodeURIComponent(stored.slice(prefix.length)));
    return isAppearancePreferences(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function serializeAppearanceCookie(preferences: AppearancePreferences): string {
  if (!isAppearancePreferences(preferences)) {
    throw new TypeError("Unsupported XRML appearance preference.");
  }
  const value = encodeURIComponent(JSON.stringify(preferences));
  return `${APPEARANCE_COOKIE_KEY}=${value}; Path=/; Max-Age=${APPEARANCE_COOKIE_MAX_AGE_SECONDS}; SameSite=Lax`;
}

export function applyAppearance(
  root: HTMLElement,
  preferences: AppearancePreferences,
  system: SystemAppearance,
): { theme: ResolvedTheme; motion: ResolvedMotion; accentAvailable: boolean } {
  const theme = resolveTheme(preferences.theme, system.prefersDark);
  const motion = resolveMotion(preferences.motion, system.prefersReducedMotion);
  const accentAvailable = preferences.accent !== "system" || system.systemAccentAvailable === true;
  const accent = preferences.accent === "system" && !accentAvailable ? "blue" : preferences.accent;

  root.dataset.themePreference = preferences.theme;
  root.dataset.theme = theme;
  root.dataset.accentPreference = preferences.accent;
  root.dataset.accent = accent;
  root.dataset.systemAccentAvailable = String(accentAvailable);
  root.dataset.density = preferences.density;
  root.dataset.motionPreference = preferences.motion;
  root.dataset.motion = motion;
  root.style.colorScheme = theme === "dark" || theme === "solarized-dark" ? "dark" : "light";
  return { theme, motion, accentAvailable };
}

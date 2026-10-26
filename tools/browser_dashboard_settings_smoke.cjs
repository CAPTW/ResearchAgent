const path = require("path");

function parseArgs(argv) {
  const parsed = {
    baseUrl: "",
    browserChannel: process.platform === "win32" ? "msedge" : "",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--base-url") {
      parsed.baseUrl = argv[index + 1] || "";
      index += 1;
    } else if (token === "--browser-channel") {
      parsed.browserChannel = argv[index + 1] || "";
      index += 1;
    }
  }
  return parsed;
}

async function main() {
  let chromium;
  try {
    ({ chromium } = require("playwright"));
  } catch (error) {
    throw new Error(
      "Cannot find module 'playwright'. Install a local Node playwright dependency or run `uv run --with playwright python tools/browser_dashboard_settings_smoke.py --base-url <url>`.",
    );
  }
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) {
    throw new Error("Missing required --base-url");
  }

  const launchCandidates = args.browserChannel
    ? [args.browserChannel]
    : process.platform === "win32"
      ? ["msedge", "chrome", ""]
      : [""];

  let browser = null;
  let launchErrors = [];
  for (const candidate of launchCandidates) {
    const launchOptions = { headless: true };
    if (candidate) {
      launchOptions.channel = candidate;
    }
    try {
      browser = await chromium.launch(launchOptions);
      break;
    } catch (error) {
      launchErrors.push(candidate ? `${candidate}: ${error.message}` : `chromium: ${error.message}`);
    }
  }
  if (!browser) {
    throw new Error(`Unable to launch a browser channel. ${launchErrors.join(" | ")}`);
  }
  const page = await browser.newPage();
  const target = new URL("/app/", args.baseUrl).toString();
  const settingsTarget = new URL("/app/settings", args.baseUrl).toString();

  try {
    await page.goto(target, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Start selected profiles" }).waitFor();
    await page.getByText("Seam prepared").waitFor();

    await page.getByRole("link", { name: "Settings" }).click();
    await page.waitForURL(settingsTarget);
    await page.getByText("Auth / Provider / Status").waitFor();
    await page.getByText("Seam prepared").waitFor();

    console.log(
      JSON.stringify(
        {
          status: "passed",
          base_url: args.baseUrl,
          dashboard_url: target,
          settings_url: settingsTarget,
          provider_mode_text: await page.getByText(/live_ready|fallback_only/).textContent(),
          stitch_text: await page.getByText("Seam prepared").textContent(),
        },
        null,
        2,
      ),
    );
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(
    JSON.stringify(
      {
        status: "failed",
        error: error instanceof Error ? error.message : String(error),
      },
      null,
      2,
    ),
  );
  process.exit(1);
});

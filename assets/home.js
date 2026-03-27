const heroElement = document.getElementById("home-hero");
const seriesGridElement = document.getElementById("series-grid");

function renderCatalog(site, seriesList) {
  document.title = site.title;

  heroElement.innerHTML = `
    <div class="eyebrow">${site.title}</div>
    <h1 class="home-title">${site.tagline}</h1>
    <p class="home-copy">${site.description}</p>
  `;

  if (!seriesList.length) {
    seriesGridElement.innerHTML = `
      <article class="series-card">
        <p class="series-summary">No live pages are published yet.</p>
      </article>
    `;
    return;
  }

  seriesGridElement.innerHTML = seriesList.map((series) => `
    <a class="series-card" href="./${series.pagePath}">
      <div class="series-card-topline">
        <div class="eyebrow">${site.title}</div>
        <div class="series-status">${series.status || "Live"}</div>
      </div>
      <h2 class="series-title">${series.title}</h2>
      <p class="series-subtitle">${series.subtitle}</p>
      <p class="series-summary">${series.summary}</p>
      <div class="series-linkline">Open live page</div>
    </a>
  `).join("");
}

async function loadCatalog() {
  try {
    const response = await fetch("./config/site.json", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Request failed with ${response.status}`);
    }

    const catalog = await response.json();
    renderCatalog(catalog.site, catalog.series);
  } catch (error) {
    heroElement.innerHTML = `
      <div class="eyebrow">Our Planet in Real-Time</div>
      <h1 class="home-title">Catalog unavailable</h1>
      <p class="home-copy">The live page index could not load right now. ${error.message}</p>
    `;
    seriesGridElement.innerHTML = "";
  }
}

loadCatalog();

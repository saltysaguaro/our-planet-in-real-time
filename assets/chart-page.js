const PAPER_COLOR = "#f7f1e7";
const PLOT_COLOR = "#fffaf4";
const monthTicks = [
  "2000-01-01",
  "2000-02-01",
  "2000-03-01",
  "2000-04-01",
  "2000-05-01",
  "2000-06-01",
  "2000-07-01",
  "2000-08-01",
  "2000-09-01",
  "2000-10-01",
  "2000-11-01",
  "2000-12-01",
];
const monthLabels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const chartElement = document.getElementById("chart");
const statusPill = document.getElementById("status-pill");
const eyebrowElement = document.getElementById("eyebrow");
const pageSummaryElement = document.getElementById("page-summary");

const pageRoot = document.body;
const basePath = pageRoot.dataset.basePath || "./";
const seriesSlug = pageRoot.dataset.seriesSlug;

const numberFormat = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 1,
});

const dateFormat = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

function renderMessage(message) {
  chartElement.innerHTML = `<div class="chart-message">${message}</div>`;
}

function asPlotPoints(calendar, values) {
  const x = [];
  const y = [];

  values.forEach((value, index) => {
    if (value === null || value === undefined) {
      return;
    }

    x.push(calendar[index]);
    y.push(value);
  });

  return { x, y };
}

function buildHistoricalTraces(payload) {
  return payload.historical.map((series, index) => {
    const points = asPlotPoints(payload.calendar, series.values);
    return {
      type: "scatter",
      mode: "lines",
      name: index === 0
        ? `${payload.metadata.historyStart}-${payload.metadata.historicalEnd}`
        : series.year.toString(),
      legendgroup: "historical",
      showlegend: index === 0,
      hovertemplate: `<b>${series.year}</b><br>%{x|%b %-d}: %{y:.1f} °C<extra></extra>`,
      x: points.x,
      y: points.y,
      line: {
        color: "rgba(17, 15, 12, 0.28)",
        width: 1.2,
      },
    };
  });
}

function buildAverageTrace(payload) {
  const points = asPlotPoints(payload.calendar, payload.average.values);
  return {
    type: "scatter",
    mode: "lines",
    name: `Average (${payload.metadata.referencePeriod[0]}-${payload.metadata.referencePeriod[1]})`,
    x: points.x,
    y: points.y,
    hovertemplate: "<b>Average</b><br>%{x|%b %-d}: %{y:.1f} °C<extra></extra>",
    line: {
      color: "rgba(93, 234, 72, 1)",
      width: 4,
    },
  };
}

function buildCurrentYearTraces(payload) {
  const points = asPlotPoints(payload.calendar, payload.currentYear.values);
  const lineTrace = {
    type: "scatter",
    mode: "lines",
    name: payload.currentYear.year.toString(),
    x: points.x,
    y: points.y,
    hovertemplate: `<b>${payload.currentYear.year}</b><br>%{x|%b %-d}: %{y:.1f} °C<extra></extra>`,
    line: {
      color: "rgba(214, 58, 31, 0.98)",
      width: 4.5,
    },
  };

  let latestIndex = -1;
  for (let index = payload.currentYear.values.length - 1; index >= 0; index -= 1) {
    const value = payload.currentYear.values[index];
    if (value !== null && value !== undefined) {
      latestIndex = index;
      break;
    }
  }

  if (latestIndex === -1) {
    return [lineTrace];
  }

  return [
    lineTrace,
    {
      type: "scatter",
      mode: "markers",
      name: "Latest point",
      showlegend: false,
      hovertemplate: "<b>Latest</b><br>%{x|%b %-d}: %{y:.1f} °C<extra></extra>",
      x: [payload.calendar[latestIndex]],
      y: [payload.currentYear.values[latestIndex]],
      marker: {
        color: "rgba(150, 35, 16, 1)",
        size: 11,
        line: {
          color: "rgba(17, 15, 12, 0.75)",
          width: 2,
        },
      },
    },
  ];
}

function buildLayout(series, payload) {
  const latestDate = new Date(`${payload.metadata.latestDataDate}T00:00:00Z`);
  const currentYearLatestDate = payload.metadata.currentYearLatestDataDate
    ? new Date(`${payload.metadata.currentYearLatestDataDate}T00:00:00Z`)
    : null;
  const titleText =
    `<span style="font-family: 'Space Grotesk', sans-serif; font-size: 1.6rem; font-weight: 700;">${series.title}</span>` +
    ` <span style="font-size: 1.05rem; color: #58524b;">${series.subtitle}</span>`;

  return {
    title: {
      text: titleText,
      x: 0.03,
      xanchor: "left",
      y: 0.98,
      yanchor: "top",
    },
    paper_bgcolor: PAPER_COLOR,
    plot_bgcolor: PLOT_COLOR,
    margin: {
      l: 78,
      r: 36,
      t: 70,
      b: 78,
    },
    font: {
      family: "'IBM Plex Sans', sans-serif",
      color: "#171412",
      size: 16,
    },
    hoverlabel: {
      bgcolor: "#fffaf4",
      bordercolor: "rgba(23, 20, 18, 0.16)",
      font: {
        family: "'IBM Plex Sans', sans-serif",
      },
    },
    legend: {
      x: 0.012,
      y: 0.99,
      bgcolor: "rgba(255, 255, 255, 0.86)",
      bordercolor: "rgba(17, 15, 12, 0.12)",
      borderwidth: 1,
      font: {
        size: 14,
      },
    },
    xaxis: {
      type: "date",
      tickmode: "array",
      tickvals: monthTicks,
      ticktext: monthLabels,
      tickfont: {
        size: 17,
      },
      linecolor: "rgba(44, 33, 16, 0.45)",
      gridcolor: "rgba(56, 50, 39, 0.12)",
      zeroline: false,
      fixedrange: false,
      range: ["2000-01-01", "2000-12-31"],
    },
    yaxis: {
      title: {
        text: "Daily Maximum Temperature (°C)",
        standoff: 14,
      },
      gridcolor: "rgba(56, 50, 39, 0.12)",
      linecolor: "rgba(44, 33, 16, 0.45)",
      zerolinecolor: "rgba(56, 50, 39, 0.2)",
      tickformat: ".0f",
      rangemode: "normal",
    },
    annotations: [
      {
        xref: "paper",
        yref: "paper",
        x: 0.995,
        y: 1.02,
        xanchor: "right",
        yanchor: "bottom",
        showarrow: false,
        align: "right",
        text: currentYearLatestDate
          ? `<b>${payload.currentYear.year} latest:</b> ${dateFormat.format(currentYearLatestDate)}`
          : `<b>Latest NOAA data:</b> ${dateFormat.format(latestDate)}`,
        font: {
          size: 13,
          color: "#4f493f",
        },
      },
      {
        xref: "paper",
        yref: "paper",
        x: 0,
        y: 0,
        xanchor: "left",
        yanchor: "bottom",
        showarrow: false,
        align: "left",
        text:
          `Data: ${payload.metadata.source}. Current line: ${payload.currentYear.year}. ` +
          `Reference period: ${payload.metadata.referencePeriod[0]}-${payload.metadata.referencePeriod[1]}. ` +
          `Updated ${dateFormat.format(new Date(payload.metadata.generatedAt))}.`,
        font: {
          size: 12,
          color: "#4f493f",
        },
      },
    ],
  };
}

function buildConfig(series) {
  return {
    responsive: true,
    displaylogo: false,
    scrollZoom: true,
    modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"],
    toImageButtonOptions: {
      format: "png",
      filename: series.imageExportName || series.slug,
      width: 1800,
      height: 1100,
      scale: 2,
    },
  };
}

async function loadCatalog() {
  const response = await fetch(`${basePath}config/site.json`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Catalog request failed with ${response.status}`);
  }
  return response.json();
}

async function loadDataset(dataPath) {
  const response = await fetch(`${basePath}${dataPath}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Dataset request failed with ${response.status}`);
  }
  return response.json();
}

async function renderChartPage() {
  renderMessage("Loading live chart...");

  try {
    if (!window.Plotly) {
      throw new Error("Plotly failed to load");
    }

    const catalog = await loadCatalog();
    const series = catalog.series.find((entry) => entry.slug === seriesSlug);

    if (!series) {
      throw new Error(`Series "${seriesSlug}" was not found in the site catalog`);
    }

    eyebrowElement.textContent = catalog.site.title;
    pageSummaryElement.textContent = series.summary;
    document.title = `${series.title} - ${series.subtitle} | ${catalog.site.title}`;

    const payload = await loadDataset(series.dataPath);
    const latestDataDate = new Date(`${payload.metadata.latestDataDate}T00:00:00Z`);
    const currentYearLatestDate = payload.metadata.currentYearLatestDataDate
      ? new Date(`${payload.metadata.currentYearLatestDataDate}T00:00:00Z`)
      : null;

    statusPill.textContent = currentYearLatestDate
      ? `${payload.currentYear.year} currently ends ${dateFormat.format(currentYearLatestDate)} at ${numberFormat.format(payload.metadata.currentYearLatestDataValue)} °C`
      : `${payload.currentYear.year} line is waiting for NOAA's first ${payload.currentYear.year} update. Latest NOAA data: ${dateFormat.format(latestDataDate)}`;

    const traces = [
      ...buildHistoricalTraces(payload),
      buildAverageTrace(payload),
      ...buildCurrentYearTraces(payload),
    ];

    await Plotly.newPlot(chartElement, traces, buildLayout(series, payload), buildConfig(series));
    window.addEventListener("resize", () => {
      Plotly.Plots.resize(chartElement);
    });
  } catch (error) {
    statusPill.textContent = "Unable to load live page";
    renderMessage(`The chart could not load right now. ${error.message}`);
  }
}

renderChartPage();

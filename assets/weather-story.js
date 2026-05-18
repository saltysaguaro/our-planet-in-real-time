const STORY_PAPER_COLOR = "#fffdf7";
const STORY_PLOT_COLOR = "#fffefa";
const STORY_GRID_COLOR = "rgba(47, 42, 32, 0.11)";
const STORY_AXIS_COLOR = "rgba(47, 42, 32, 0.34)";
const STORY_TEXT_COLOR = "#171412";
const TEMPERATURE_AXIS_MAX_C = 50;

const storyRoot = document.body;
const storyBasePath = storyRoot.dataset.basePath || "./";
const storySeriesSlug = storyRoot.dataset.seriesSlug;

const statusElement = document.getElementById("weather-story-status");
const eyebrowElement = document.getElementById("weather-story-eyebrow");
const titleElement = document.getElementById("weather-story-title");
const summaryElement = document.getElementById("weather-story-summary");
const chartsElement = document.getElementById("weather-story-charts");

const storyDateFormat = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

function renderStoryMessage(message) {
  chartsElement.innerHTML = `<div class="chart-message">${message}</div>`;
}

function asUtcDate(dateText) {
  return new Date(`${dateText}T00:00:00Z`);
}

function formatSignedCoordinate(value, positiveSuffix, negativeSuffix) {
  const suffix = value >= 0 ? positiveSuffix : negativeSuffix;
  return `${Math.abs(value).toFixed(2)}°${suffix}`;
}

function getStoryWindows(payload) {
  if (Array.isArray(payload.metadata.windows) && payload.metadata.windows.length) {
    return payload.metadata.windows;
  }

  return [
    {
      id: "current",
      label: "Current",
      kind: "trailing-365",
      startDate: payload.metadata.startDate,
      endDate: payload.metadata.endDate,
      days: payload.metadata.windowDays,
    },
  ];
}

function getDefaultWindowId(payload, windows) {
  return payload.metadata.defaultWindowId || windows[0].id;
}

function getLocationWindow(location, windowId, payload) {
  if (location.windows && location.windows[windowId]) {
    return location.windows[windowId];
  }

  return {
    dates: payload.dates,
    values: location.values,
  };
}

function createLocationSection(location, windows, defaultWindowId) {
  const section = document.createElement("section");
  section.className = "weather-location-section";

  const heading = document.createElement("div");
  heading.className = "weather-location-heading";

  const title = document.createElement("h2");
  title.className = "weather-location-title";
  title.textContent = location.name;

  const meta = document.createElement("div");
  meta.className = "weather-location-meta";
  meta.textContent = [
    formatSignedCoordinate(location.latitude, "N", "S"),
    formatSignedCoordinate(location.longitude, "E", "W"),
  ].join(", ");

  const controls = document.createElement("div");
  controls.className = "weather-location-controls";

  const selectorLabel = document.createElement("label");
  selectorLabel.className = "weather-window-label";
  selectorLabel.htmlFor = `weather-window-${location.id}`;
  selectorLabel.textContent = "Period";

  const selector = document.createElement("select");
  selector.id = `weather-window-${location.id}`;
  selector.className = "weather-window-select";
  selector.setAttribute("aria-label", `Weather period for ${location.name}`);

  windows.forEach((windowOption) => {
    const option = document.createElement("option");
    option.value = windowOption.id;
    option.textContent = windowOption.label;
    selector.append(option);
  });
  selector.value = defaultWindowId;

  const chart = document.createElement("div");
  chart.id = `weather-chart-${location.id}`;
  chart.className = "weather-location-chart";
  chart.setAttribute("aria-label", `${location.name} daily weather chart`);

  selectorLabel.append(selector);
  controls.append(meta, selectorLabel);
  heading.append(title, controls);
  section.append(heading, chart);
  chartsElement.append(section);

  return { chart, selector };
}

function buildLocationTraces(locationWindow) {
  const dates = locationWindow.dates;
  const values = locationWindow.values;

  return [
    {
      type: "scatter",
      mode: "lines",
      name: "Mean humidity",
      legendrank: 3,
      zorder: 0,
      x: dates,
      y: values.relativeHumidityMeanPct,
      xaxis: "x",
      yaxis: "y2",
      fill: "tozeroy",
      fillcolor: "rgba(77, 139, 69, 0.12)",
      hovertemplate: "%{x|%b %-d, %Y}<br>Humidity: %{y:.0f}%<extra></extra>",
      line: {
        color: "rgba(77, 139, 69, 0)",
        width: 0,
      },
    },
    {
      type: "bar",
      name: "Precipitation",
      legendrank: 4,
      zorder: 1,
      x: dates,
      y: values.precipitationSumMm,
      xaxis: "x",
      yaxis: "y3",
      hovertemplate: "%{x|%b %-d, %Y}<br>Precipitation: %{y:.1f} mm<extra></extra>",
      marker: {
        color: "rgba(56, 112, 166, 0.7)",
      },
    },
    {
      type: "scatter",
      mode: "lines",
      name: "High temperature",
      legendrank: 1,
      zorder: 3,
      x: dates,
      y: values.temperatureMaxC,
      xaxis: "x",
      yaxis: "y",
      hovertemplate: "%{x|%b %-d, %Y}<br>High: %{y:.1f} °C<extra></extra>",
      line: {
        color: "#c84a2f",
        width: 2.3,
      },
    },
    {
      type: "scatter",
      mode: "lines",
      name: "Low temperature",
      legendrank: 2,
      zorder: 3,
      x: dates,
      y: values.temperatureMinC,
      xaxis: "x",
      yaxis: "y",
      hovertemplate: "%{x|%b %-d, %Y}<br>Low: %{y:.1f} °C<extra></extra>",
      line: {
        color: "#287c9f",
        width: 2.1,
      },
    },
  ];
}

function valuesRange(values, step, padding, fallbackMaximum) {
  const finiteValues = values.filter((value) => Number.isFinite(value));
  if (!finiteValues.length) {
    return [0, fallbackMaximum];
  }

  const minimum = Math.floor((Math.min(...finiteValues) - padding) / step) * step;
  let maximum = Math.ceil((Math.max(...finiteValues) + padding) / step) * step;

  if (minimum === maximum) {
    maximum = minimum + step;
  }

  return [minimum, maximum];
}

function buildTemperatureRange(payload) {
  const temperatureValues = payload.locations.flatMap((location) => {
    const windows = location.windows ? Object.values(location.windows) : [getLocationWindow(location, "current", payload)];
    return windows.flatMap((locationWindow) => ([
      ...locationWindow.values.temperatureMaxC,
      ...locationWindow.values.temperatureMinC,
    ]));
  });
  const [minimum] = valuesRange(temperatureValues, 5, 2, TEMPERATURE_AXIS_MAX_C);
  return [Math.min(0, minimum), TEMPERATURE_AXIS_MAX_C];
}

function buildLocationRanges(locationWindow, payload) {
  const values = locationWindow.values;
  const precipitationRange = valuesRange(values.precipitationSumMm, 5, 1, 5);
  precipitationRange[0] = 0;

  return {
    temperatureC: buildTemperatureRange(payload),
    relativeHumidityPct: [0, 100],
    precipitationMm: precipitationRange,
  };
}

function buildLocationLayout(series, payload, locationWindow) {
  const ranges = buildLocationRanges(locationWindow, payload);
  const dates = locationWindow.dates || [];
  const dateRange = [dates[0], dates[dates.length - 1]];

  return {
    paper_bgcolor: STORY_PAPER_COLOR,
    plot_bgcolor: STORY_PLOT_COLOR,
    margin: {
      l: 58,
      r: 92,
      t: 46,
      b: 64,
    },
    font: {
      family: "'IBM Plex Sans', sans-serif",
      color: STORY_TEXT_COLOR,
      size: 14,
    },
    hovermode: "x unified",
    dragmode: false,
    hoverlabel: {
      bgcolor: "#fffefa",
      bordercolor: "rgba(23, 20, 18, 0.16)",
      font: {
        family: "'IBM Plex Sans', sans-serif",
      },
    },
    legend: {
      orientation: "h",
      x: 0,
      y: 1.1,
      xanchor: "left",
      yanchor: "bottom",
      bgcolor: "rgba(255, 254, 250, 0.86)",
      font: {
        size: 13,
      },
      itemwidth: 30,
      itemclick: false,
      itemdoubleclick: false,
    },
    bargap: 0.06,
    xaxis: {
      domain: [0, 0.925],
      type: "date",
      range: dateRange,
      tickformat: "%b %-d",
      tickfont: {
        size: 13,
      },
      linecolor: STORY_AXIS_COLOR,
      gridcolor: STORY_GRID_COLOR,
      zeroline: false,
      fixedrange: true,
    },
    yaxis: {
      range: ranges.temperatureC,
      title: {
        text: "Temperature (°C)",
        standoff: 8,
        font: {
          color: STORY_TEXT_COLOR,
        },
      },
      automargin: true,
      tickformat: ".0f",
      side: "left",
      linecolor: STORY_AXIS_COLOR,
      gridcolor: STORY_GRID_COLOR,
      zerolinecolor: "rgba(47, 42, 32, 0.18)",
      fixedrange: true,
    },
    yaxis2: {
      overlaying: "y",
      anchor: "free",
      side: "right",
      position: 0.955,
      range: ranges.relativeHumidityPct,
      title: {
        text: "Humidity (%)",
        standoff: 4,
        font: {
          color: "#4d8b45",
          size: 12,
        },
      },
      automargin: true,
      tickformat: ".0f",
      ticksuffix: "%",
      tickfont: {
        color: "#4d8b45",
        size: 12,
      },
      linecolor: "rgba(77, 139, 69, 0.55)",
      gridcolor: "rgba(77, 139, 69, 0)",
      zeroline: false,
      fixedrange: true,
    },
    yaxis3: {
      overlaying: "y",
      anchor: "free",
      side: "right",
      position: 1,
      range: ranges.precipitationMm,
      title: {
        text: "Precipitation (mm)",
        standoff: 34,
        font: {
          color: "#3870a6",
          size: 12,
        },
      },
      automargin: true,
      tickformat: ".0f",
      tickfont: {
        color: "#3870a6",
        size: 12,
      },
      linecolor: "rgba(56, 112, 166, 0.55)",
      gridcolor: "rgba(56, 112, 166, 0)",
      zerolinecolor: "rgba(47, 42, 32, 0.18)",
      fixedrange: true,
    },
    annotations: [
      {
        xref: "paper",
        yref: "paper",
        x: 1,
        y: -0.12,
        xanchor: "right",
        yanchor: "top",
        showarrow: false,
        align: "right",
        text: `Data: ${payload.metadata.source}`,
        font: {
          size: 11,
          color: "#5f5b56",
        },
      },
    ],
  };
}

function buildLocationConfig(series, location) {
  return {
    responsive: true,
    displayModeBar: false,
    displaylogo: false,
    scrollZoom: false,
    doubleClick: false,
    modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"],
    toImageButtonOptions: {
      format: "png",
      filename: `${series.imageExportName || series.slug}-${location.id}`,
      width: 1800,
      height: 1200,
      scale: 2,
    },
  };
}

function getWindowDateText(windowOption) {
  return `${storyDateFormat.format(asUtcDate(windowOption.startDate))} to ${storyDateFormat.format(asUtcDate(windowOption.endDate))}`;
}

function renderLocationChart(series, payload, entry, windowId) {
  const locationWindow = getLocationWindow(entry.location, windowId, payload);
  return Plotly.react(
    entry.element,
    buildLocationTraces(locationWindow),
    buildLocationLayout(series, payload, locationWindow),
    buildLocationConfig(series, entry.location),
  );
}

async function loadWeatherCatalog() {
  const response = await fetch(`${storyBasePath}config/site.json`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Catalog request failed with ${response.status}`);
  }
  return response.json();
}

async function loadWeatherDataset(dataPath) {
  const response = await fetch(`${storyBasePath}${dataPath}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Dataset request failed with ${response.status}`);
  }
  return response.json();
}

async function renderWeatherStory() {
  renderStoryMessage("Loading weather charts...");

  try {
    if (!window.Plotly) {
      throw new Error("Plotly failed to load");
    }

    const catalog = await loadWeatherCatalog();
    const series = catalog.series.find((entry) => entry.slug === storySeriesSlug);
    if (!series) {
      throw new Error(`Series "${storySeriesSlug}" was not found in the site catalog`);
    }

    const payload = await loadWeatherDataset(series.dataPath);
    const windows = getStoryWindows(payload);
    const defaultWindowId = getDefaultWindowId(payload, windows);
    const defaultWindow = windows.find((windowOption) => windowOption.id === defaultWindowId) || windows[0];
    const generatedAt = storyDateFormat.format(new Date(payload.metadata.generatedAt));

    document.title = `${series.title} - ${series.subtitle} | ${catalog.site.title}`;
    eyebrowElement.textContent = catalog.site.title;
    titleElement.textContent = series.title;
    summaryElement.textContent = series.summary;
    statusElement.textContent = `${defaultWindow.label}: ${getWindowDateText(defaultWindow)}; updated ${generatedAt}`;

    chartsElement.innerHTML = "";
    const chartEntries = payload.locations.map((location) => {
      const { chart, selector } = createLocationSection(location, windows, defaultWindowId);
      const entry = {
        location,
        element: chart,
        selector,
        selectedWindowId: defaultWindowId,
      };

      selector.addEventListener("change", () => {
        entry.selectedWindowId = selector.value;
        renderLocationChart(series, payload, entry, entry.selectedWindowId).catch((error) => {
          statusElement.textContent = "Unable to update weather chart";
          console.error(error);
        });
      });

      return entry;
    });

    await Promise.all(chartEntries.map((entry) => renderLocationChart(series, payload, entry, entry.selectedWindowId)));

    window.addEventListener("resize", () => {
      chartEntries.forEach(({ element }) => Plotly.Plots.resize(element));
    });
  } catch (error) {
    statusElement.textContent = "Unable to load weather story";
    renderStoryMessage(`The weather charts could not load right now. ${error.message}`);
  }
}

renderWeatherStory();

(() => {
  const form = document.querySelector("#track-form");
  const input = document.querySelector("#youtube-url");
  const submitButton = document.querySelector("#submit-button");
  const statusPanel = document.querySelector("#status-panel");
  const statusMessage = document.querySelector("#status-message");
  const statusPercent = document.querySelector("#status-percent");
  const statusEta = document.querySelector("#status-eta");
  const progressBar = document.querySelector("#progress-bar");
  const timelineTrack = document.querySelector("#timeline-track");
  const timelineCursor = document.querySelector("#timeline-cursor");
  const timelineMeta = document.querySelector("#timeline-meta");
  const cancelButton = document.querySelector("#cancel-button");
  const statusStages = document.querySelector("#status-stages");
  const errorPanel = document.querySelector("#error-panel");
  const errorMessage = document.querySelector("#error-message");
  const results = document.querySelector("#results");
  const trackList = document.querySelector("#track-list");
  const exportMenu = document.querySelector(".export-menu");
  const shareButton = document.querySelector("#share-button");
  const newButton = document.querySelector("#new-button");
  const retryButton = document.querySelector("#retry-button");
  const cacheState = document.querySelector("#cache-state");

  const order = ["queued", "inspecting", "downloading", "recognizing", "rescanning"];
  const state = {
    jobId: null,
    eventSource: null,
    startedAt: 0,
    currentJob: null,
    progressSamples: [],
    duration: null,
  };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const formatBytes = (n) =>
    n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 ** 2).toFixed(1)} MB`;
  const formatEta = (seconds) => {
    if (!Number.isFinite(seconds) || seconds <= 0) return "almost done";
    if (seconds < 60) return `< 1 min remaining`;
    if (seconds < 3600) return `~ ${Math.round(seconds / 60)} min remaining`;
    return `~ ${(seconds / 3600).toFixed(1)} h remaining`;
  };

  const url = new URL(window.location.href);
  const queryJob = url.searchParams.get("id");
  if (queryJob) {
    state.jobId = queryJob;
    resumeJob(queryJob);
  } else {
    const stored = localStorage.getItem("trackid-job");
    if (stored) {
      state.jobId = stored;
      resumeJob(stored);
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = input.value.trim();
    if (!url) return;
    const mode = form.querySelector("input[name=mode]:checked")?.value || "thorough";
    closeStream();
    resetView();
    setSubmitting(true);
    try {
      const response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, mode }),
      });
      const job = await readJson(response);
      state.startedAt = Date.now();
      state.jobId = job.id;
      localStorage.setItem("trackid-job", job.id);
      updateHistoryUrl(job.id);
      openStream(job.id);
    } catch (error) {
      showError(error.message);
    } finally {
      setSubmitting(false);
    }
  });

  cancelButton.addEventListener("click", async () => {
    if (!state.jobId) return;
    cancelButton.disabled = true;
    try {
      await fetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
    } catch {
      cancelButton.disabled = false;
    }
  });

  retryButton.addEventListener("click", () => startAgain());
  newButton.addEventListener("click", () => startAgain());

  shareButton.addEventListener("click", async () => {
    if (!state.jobId) return;
    const link = `${window.location.origin}/?id=${state.jobId}`;
    try {
      await navigator.clipboard.writeText(link);
      flashButton(shareButton, "Link copied");
    } catch {
      window.prompt("Share URL", link);
    }
  });

  exportMenu.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-format]");
    if (!button) return;
    downloadExport(button.dataset.format);
    exportMenu.removeAttribute("open");
  });

  document.addEventListener("click", (event) => {
    const menu = event.target.closest(".track-row-menu");
    document.querySelectorAll(".track-row-menu[data-open]").forEach((node) => {
      if (node !== menu) node.removeAttribute("data-open");
    });
    const trigger = event.target.closest(".track-row-menu-button");
    if (!trigger) return;
    const wrapper = trigger.parentElement;
    const willOpen = !wrapper.hasAttribute("data-open");
    document.querySelectorAll(".track-row-menu[data-open]").forEach((node) =>
      node.removeAttribute("data-open")
    );
    if (willOpen) wrapper.setAttribute("data-open", "");
  });

  function openStream(jobId) {
    closeStream();
    const source = new EventSource(`/api/jobs/${jobId}/events`);
    state.eventSource = source;
    source.addEventListener("queued", handleEvent);
    source.addEventListener("inspecting", handleEvent);
    source.addEventListener("downloading", handleEvent);
    source.addEventListener("recognizing", handleEvent);
    source.addEventListener("rescanning", handleEvent);
    source.addEventListener("completed", handleCompleted);
    source.addEventListener("failed", handleFailed);
    source.addEventListener("cancelled", handleFailed);
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) return;
      setTimeout(() => pollJob(jobId), 1500);
    };
  }

  function closeStream() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
  }

  async function pollJob(jobId) {
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      const job = await readJson(response);
      if (job.status === "completed") {
        handleCompleted({ data: JSON.stringify(job) });
      } else if (job.status === "failed") {
        handleFailed({ data: JSON.stringify({ message: job.error }) });
      } else {
        renderStatus(job);
      }
    } catch (error) {
      /* keep connection retrying */
    }
    await sleep(2000);
    if (state.jobId === jobId && !state.eventSource) pollJob(jobId);
  }

  async function resumeJob(jobId) {
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      if (response.status === 404) {
        localStorage.removeItem("trackid-job");
        state.jobId = null;
        return;
      }
      const job = await readJson(response);
      if (job.status === "completed") {
        state.currentJob = job;
        showResults(job);
        return;
      }
      if (job.status === "failed") {
        showError(job.error || "Analysis failed");
        return;
      }
      openStream(jobId);
    } catch {
      /* ignore */
    }
  }

  function handleEvent(event) {
    try {
      const payload = JSON.parse(event.data);
      renderProgress(payload);
      if (!state.duration && payload.status === "recognizing") {
        /* duration inferred when transitions start appearing */
      }
    } catch {
      /* malformed */
    }
  }

  async function handleCompleted(event) {
    closeStream();
    try {
      const response = await fetch(`/api/jobs/${state.jobId}`);
      const job = await readJson(response);
      state.currentJob = job;
      localStorage.removeItem("trackid-job");
      showResults(job);
    } catch (error) {
      showError(error.message);
    }
  }

  function handleFailed(event) {
    closeStream();
    try {
      const payload = JSON.parse(event.data);
      showError(payload.message || "Analysis could not be completed");
    } catch {
      showError("Analysis could not be completed");
    }
    localStorage.removeItem("trackid-job");
  }

  function renderProgress(payload) {
    const isActive = (status) => order.indexOf(status) >= 0;
    if (!isActive(payload.status)) return;
    errorPanel.classList.add("is-hidden");
    results.classList.add("is-hidden");
    statusPanel.classList.remove("is-hidden");
    statusMessage.textContent = payload.message;
    statusPercent.textContent = `${payload.progress}%`;
    progressBar.style.width = `${payload.progress}%`;
    timelineTrack.style.setProperty("--progress", `${payload.progress}%`);
    timelineCursor.style.left = `${payload.progress}%`;
    const idx = order.indexOf(payload.status);
    statusStages.querySelectorAll("span").forEach((stage, index) => {
      stage.classList.toggle("is-active", index === idx);
      stage.classList.toggle("is-done", idx > index || payload.status === "completed");
    });
    timelineMeta.textContent = statusLabel(payload);
    estimateEta(payload);
    statusPanel.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async function renderStatus(job) {
    renderProgress({
      status: job.status,
      progress: job.progress,
      message: job.message,
    });
  }

  function estimateEta(payload) {
    if (payload.progress < 5 || payload.status !== "recognizing") {
      statusEta.textContent = "estimating ETA";
      return;
    }
    const elapsed = (Date.now() - state.startedAt) / 1000;
    const rate = payload.progress / Math.max(elapsed, 0.001);
    const remainingSeconds = (100 - payload.progress) / rate;
    state.progressSamples.push({ t: Date.now(), progress: payload.progress });
    if (state.progressSamples.length > 12) state.progressSamples.shift();
    statusEta.textContent = formatEta(remainingSeconds);
  }

  function statusLabel(payload) {
    if (payload.status === "recognizing") return `Recognizing samples (${payload.progress}% done)`;
    return payload.message;
  }

  function showResults(job) {
    statusPanel.classList.add("is-hidden");
    errorPanel.classList.add("is-hidden");
    results.classList.remove("is-hidden");
    const metadata = job.metadata;
    const thumbnail = document.querySelector("#video-thumbnail");
    thumbnail.src = metadata.thumbnail_url || "";
    thumbnail.alt = `Thumbnail of ${metadata.title}`;
    document.querySelector("#video-title").textContent = metadata.title;
    document.querySelector("#video-channel").textContent = metadata.channel || "Unknown channel";
    document.querySelector("#video-duration").textContent = metadata.duration;
    document.querySelector("#track-count").textContent = `${job.tracks.length} tracks`;
    cacheState.classList.toggle("is-hidden", !job.cache_hit);
    if (job.tracks.length && metadata.duration_seconds) {
      const positions = job.tracks
        .filter((track) => track.timestamp_seconds > 0)
        .map((track) => track.timestamp_seconds / metadata.duration_seconds * 100);
      timelineTrack.style.setProperty("--progress", "100%");
      timelineCursor.style.left = "100%";
      renderTimelineMarkers(positions);
    }
    trackList.replaceChildren(...job.tracks.map((track) => createTrackRow(track, metadata)));
    results.scrollIntoView({ behavior: "smooth", block: "start" });
    state.duration = metadata.duration_seconds;
  }

  function renderTimelineMarkers(positions) {
    document.querySelectorAll(".timeline-marker").forEach((node) => node.remove());
    for (const pct of positions) {
      const marker = document.createElement("span");
      marker.className = "timeline-marker";
      marker.style.left = `${pct.toFixed(2)}%`;
      if (pct >= 99) marker.dataset.active = "true";
      timelineTrack.appendChild(marker);
    }
  }

  function createTrackRow(track, metadata) {
    const row = document.createElement("article");
    row.className = "track-row";
    row.dataset.trackId = track.position;

    const number = document.createElement("span");
    number.className = "track-number";
    number.textContent = String(track.position).padStart(2, "0");

    const main = document.createElement("div");
    main.className = "track-main";
    const cover = document.createElement("div");
    cover.className = "track-cover";
    if (track.cover_url) {
      const image = document.createElement("img");
      image.src = track.cover_url;
      image.alt = "";
      image.loading = "lazy";
      cover.append(image);
    }
    const name = document.createElement("div");
    name.className = "track-name";
    const titleElement = document.createElement("strong");
    titleElement.textContent = track.title;
    if (track.mix) {
      const mixTag = document.createElement("span");
      mixTag.className = "mix-tag";
      mixTag.textContent = track.mix;
      titleElement.append(mixTag);
    }
    const artist = document.createElement("span");
    artist.className = "track-artist";
    artist.textContent = track.artist || "Unknown artist";
    name.append(titleElement, artist);
    main.append(cover, name);

    const timestamp = document.createElement("a");
    timestamp.className = "track-time";
    timestamp.href = `${metadata.youtube_url}&t=${track.timestamp_seconds}s`;
    timestamp.target = "_blank";
    timestamp.rel = "noopener noreferrer";
    timestamp.textContent = track.timestamp;
    timestamp.title = "Open this moment on YouTube";

    const source = document.createElement("span");
    source.className = "source-pill";
    source.textContent = track.source === "shazam" ? "Audio ID" : "YouTube";
    if (track.source === "shazam") source.title = "Identified via Shazam fingerprint";

    const menu = document.createElement("div");
    menu.className = "track-row-menu";
    const menuButton = document.createElement("button");
    menuButton.className = "track-row-menu-button";
    menuButton.type = "button";
    menuButton.setAttribute("aria-label", "Track actions");
    menuButton.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="3" cy="8" r="1.4"/><circle cx="8" cy="8" r="1.4"/><circle cx="13" cy="8" r="1.4"/></svg>';
    const list = document.createElement("div");
    list.className = "track-row-menu-list";
    const rescan = document.createElement("button");
    rescan.type = "button";
    rescan.innerHTML = "Rescan this section<small>Re-identify ±2 min around this moment</small>";
    rescan.addEventListener("click", () => requestRescan(track, metadata));
    const copyLink = document.createElement("button");
    copyLink.type = "button";
    copyLink.textContent = "Copy timestamp link";
    copyLink.addEventListener("click", () => copyTimestamp(track, metadata, copyLink));
    list.append(rescan, copyLink);
    menu.append(menuButton, list);

    row.append(number, main, timestamp, source, menu);
    return row;
  }

  async function requestRescan(track, metadata) {
    if (!state.jobId) return;
    const start = Math.max(0, track.timestamp_seconds - 120);
    const end = Math.min(metadata.duration_seconds, track.timestamp_seconds + 120);
    try {
      const response = await fetch(`/api/jobs/${state.jobId}/rescan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start, end }),
      });
      if (!response.ok) throw new Error("Could not queue rescan");
      openStream(state.jobId);
    } catch (error) {
      alert(error.message);
    }
  }

  async function copyTimestamp(track, metadata, button) {
    const link = `${metadata.youtube_url}&t=${track.timestamp_seconds}s`;
    try {
      await navigator.clipboard.writeText(link);
      flashButton(button, "Copied");
    } catch {
      window.prompt("Copy link", link);
    }
  }

  function flashButton(button, message) {
    const original = button.textContent;
    button.textContent = message;
    setTimeout(() => {
      button.textContent = original;
    }, 1600);
  }

  async function downloadExport(format) {
    if (!state.jobId) return;
    const response = await fetch(`/api/jobs/${state.jobId}/export?format=${format}`);
    if (!response.ok) return;
    const blob = await response.blob();
    const link = document.createElement("a");
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    link.download = match ? match[1] : `tracklist.${format}`;
    link.href = URL.createObjectURL(blob);
    link.click();
    URL.revokeObjectURL(link.href);
  }

  function showError(message) {
    state.jobId = null;
    errorMessage.textContent = message;
    errorPanel.classList.remove("is-hidden");
    statusPanel.classList.add("is-hidden");
    results.classList.add("is-hidden");
    errorPanel.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function resetView() {
    state.duration = null;
    state.progressSamples = [];
    errorPanel.classList.add("is-hidden");
    results.classList.add("is-hidden");
    document.querySelectorAll(".timeline-marker").forEach((node) => node.remove());
  }

  function startAgain() {
    state.jobId = null;
    state.currentJob = null;
    state.duration = null;
    closeStream();
    localStorage.removeItem("trackid-job");
    updateHistoryUrl(null);
    resetView();
    input.focus();
    document.querySelector("#start").scrollIntoView({ behavior: "smooth" });
  }

  function updateHistoryUrl(jobId) {
    const next = new URL(window.location.href);
    if (jobId) next.searchParams.set("id", jobId);
    else next.searchParams.delete("id");
    window.history.replaceState({}, "", next);
  }

  function readJson(response) {
    if (!response.ok) {
      return response.json().catch(() => ({ detail: "Request failed" })).then((data) => {
        throw new Error(data.detail || "Request failed");
      });
    }
    return response.json();
  }

  function setSubmitting(submitting) {
    submitButton.disabled = submitting;
    submitButton.querySelector("span").textContent = submitting ? "Starting…" : "Generate tracklist";
  }

  const SOCIAL_DISMISS_KEY = "trackid-social-dismissed";
  const SOCIAL_DELAY = 6500;
  const followFab = document.querySelector("#follow-fab");
  const followModal = document.querySelector("#follow-modal");
  const followModalClose = document.querySelector("#follow-modal-close");
  const followModalBackdrop = document.querySelector("#follow-modal-backdrop");
  const followModalDismiss = document.querySelector("#follow-modal-dismiss");

  function socialDismissed() {
    try {
      return localStorage.getItem(SOCIAL_DISMISS_KEY) === "1";
    } catch {
      return true;
    }
  }

  function persistSocialDismiss() {
    try {
      localStorage.setItem(SOCIAL_DISMISS_KEY, "1");
    } catch {
      /* private mode */
    }
  }

  function openFollowModal() {
    if (socialDismissed()) return;
    followModal?.classList.remove("is-hidden");
    document.body.style.overflow = "hidden";
  }

  function closeFollowModal() {
    followModal?.classList.add("is-hidden");
    document.body.style.overflow = "";
  }

  function handleSocialClick(event) {
    if (socialDismissed()) return;
    persistSocialDismiss();
    followFab?.classList.add("is-hidden");
  }

  function startSocialNudge() {
    if (socialDismissed()) return;
    followFab?.classList.remove("is-hidden");
    setTimeout(openFollowModal, SOCIAL_DELAY);
  }

  followFab?.addEventListener("click", openFollowModal);
  followModalClose?.addEventListener("click", closeFollowModal);
  followModalBackdrop?.addEventListener("click", closeFollowModal);
  followModalDismiss?.addEventListener("click", () => {
    persistSocialDismiss();
    closeFollowModal();
    followFab?.classList.add("is-hidden");
  });
  document.querySelectorAll(".site-nav a.social-icon, .follow-button").forEach((link) =>
    link.addEventListener("click", handleSocialClick)
  );
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && followModal && !followModal.classList.contains("is-hidden")) {
      closeFollowModal();
    }
  });
  window.addEventListener("load", startSocialNudge, { once: true });
})();

/* Scholar Hunter — frontend logic. Plain JS, no framework, no build step. */

(function () {
  "use strict";

  const form = document.getElementById("profile-form");
  const submitBtn = document.getElementById("submit-btn");
  const profileView = document.getElementById("profile-view");
  const resultsView = document.getElementById("results-view");
  const profileStrip = document.getElementById("profile-strip");
  const editProfileBtn = document.getElementById("edit-profile");
  const backlinkRow = document.getElementById("backlink-row");
  const backToResultsBtn = document.getElementById("back-to-results");
  const loading = document.getElementById("loading");
  const loadingText = document.getElementById("loading-text");
  const resultsEl = document.getElementById("results");
  const summaryEl = document.getElementById("results-summary");
  const errorBanner = document.getElementById("error-banner");
  const errorText = document.getElementById("error-text");

  // The two pages of the app. The form is only hidden, never destroyed, so
  // editing the profile keeps everything the student already typed.
  let hasShortlist = false;

  function showProfileView() {
    resultsView.hidden = true;
    profileView.hidden = false;
    backlinkRow.hidden = !hasShortlist;
    window.scrollTo({ top: 0 });
  }

  function showResultsView() {
    profileView.hidden = true;
    resultsView.hidden = false;
    window.scrollTo({ top: 0 });
  }

  // What the shortlist was audited against, restated on the results page.
  function profileSummary() {
    const parts = [
      form.elements.field_of_study.value.trim(),
      form.elements.degree_level.value,
      form.elements.nationality.value.trim(),
    ].filter(Boolean);
    const gpa = form.elements.gpa.value.trim();
    if (gpa) parts.push("GPA " + gpa);
    const interests = form.elements.interests.value.trim();
    if (interests) parts.push(interests);
    return parts.join(" · ");
  }

  editProfileBtn.addEventListener("click", showProfileView);
  backToResultsBtn.addEventListener("click", showResultsView);

  // Whether Gmail drafting is configured. Checked once so the Draft button can
  // explain itself instead of failing on click.
  let gmailReady = true;

  // The profile from the last search, so document guidance can be tailored to
  // this student rather than handing out generic advice.
  let lastProfile = {};

  fetch("/health")
    .then((r) => r.json())
    .then((state) => {
      gmailReady = Boolean(state.gmail_drafting);
    })
    .catch(() => {
      /* health is a nicety; the form still works without it */
    });

  // --- helpers -----------------------------------------------------------

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function isStated(value) {
    const text = String(value == null ? "" : value).trim().toLowerCase();
    return text !== "" && text !== "not stated" && text !== "none";
  }

  function showError(message) {
    errorText.textContent = message;
    errorBanner.hidden = false;
    errorBanner.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function clearError() {
    errorBanner.hidden = true;
  }

  /* Parse a deadline written for humans ("15 January 2027") and report how many
     days away it is. Returns null when we cannot read it — better to show the
     text plainly than to invent an urgency that is not there. */
  function daysUntil(text) {
    if (!isStated(text)) return null;
    const parsed = Date.parse(String(text).replace(/(\d+)(st|nd|rd|th)/gi, "$1"));
    if (Number.isNaN(parsed)) return null;
    return Math.round((parsed - Date.now()) / 86400000);
  }

  // What the agent is doing, narrated while the student waits.
  const LOADING_STEPS = [
    "Searching trusted sources…",
    "Opening each opportunity's own page…",
    "Checking requirements one at a time…",
    "Stamping what is met, not met, or not stated…",
  ];
  let loadingTimer = null;

  function startLoadingNarration() {
    let step = 0;
    loadingText.textContent = LOADING_STEPS[0];
    loadingTimer = setInterval(() => {
      step = (step + 1) % LOADING_STEPS.length;
      loadingText.textContent = LOADING_STEPS[step];
    }, 3500);
  }

  function stopLoadingNarration() {
    clearInterval(loadingTimer);
    loadingTimer = null;
  }

  // --- rendering ---------------------------------------------------------

  const VERDICT_TEXT = {
    eligible: { icon: "✓", label: "Eligible" },
    unclear: { icon: "?", label: "Unclear" },
    not_eligible: { icon: "✕", label: "Not eligible" },
  };

  const STATUS_MARK = { met: "✓", not_met: "✕", not_stated: "–" };
  const STATUS_WORD = {
    met: "met",
    not_met: "not met",
    not_stated: "not stated",
  };

  function renderRequirements(rows) {
    if (!rows || !rows.length) {
      return '<p class="card__hint" style="margin:0">This page states no checkable requirements.</p>';
    }
    const items = rows
      .map((row) => {
        const mark = STATUS_MARK[row.status] || "–";
        const word = STATUS_WORD[row.status] || row.status;
        return `
        <li>
          <div class="ledger__row">
            <span class="ledger__key">${escapeHtml(row.label)}</span>
            <span class="ledger__val">${escapeHtml(row.required)}</span>
            <span class="ledger__leader" aria-hidden="true"></span>
            <span class="stamp stamp--${escapeHtml(row.status)}">${mark} ${escapeHtml(word)}</span>
          </div>
          ${row.note ? `<p class="ledger__note">${escapeHtml(row.note)}</p>` : ""}
        </li>`;
      })
      .join("");
    return `<ul class="ledger reqs">${items}</ul>`;
  }

  function renderCourseMatch(match) {
    if (!match || !match.assessed) {
      return `<p class="course-match">Not assessed${
        match && match.summary === "not assessed"
          ? " — add your courses in the profile, or this programme lists no prerequisites."
          : "."
      }</p>`;
    }
    const pct = match.total ? Math.round((match.matched / match.total) * 100) : 0;
    const note = match.confidence_capped
      ? '<span class="course-match__note"> Confidence is capped: course names were given without descriptions.</span>'
      : "";
    return `
      <p class="course-match">
        <strong>${escapeHtml(match.summary)}</strong>${note}
      </p>
      <div class="bar" role="img" aria-label="${escapeHtml(match.summary)}">
        <div class="bar__fill" style="width:${pct}%"></div>
      </div>`;
  }

  function renderDeadline(item) {
    if (!isStated(item.deadline)) {
      return '<span class="ledger__val">Not stated</span>';
    }
    const text = escapeHtml(item.deadline);
    const days = daysUntil(item.deadline);

    if (item.deadline_status === "expired" || (days !== null && days < 0)) {
      return `<span class="ledger__val">${text}</span>
        <span class="deadline--expired">closed</span>`;
    }
    if (days !== null && days <= 30) {
      return `<span class="ledger__val">${text}</span>
        <span class="deadline--soon">${days} day${days === 1 ? "" : "s"} left</span>`;
    }
    return `<span class="ledger__val">${text}</span>`;
  }

  /* Documents the application asks for. Rendered as a stamped checklist so it
     reads like the rest of the audit rather than a bare bullet list. */
  /* Each required document gets its own file input, in the programme's order.
     Attaching per requirement means the pack is assembled correctly without
     anyone having to guess which uploaded file is which. */
  function renderDocuments(documents) {
    if (!documents || !documents.length) {
      return '<p class="muted-note">This page does not list the documents required — check the opportunity\'s own application page before applying.</p>';
    }
    const items = documents
      .map(
        (doc, i) => `
        <li>
          <span class="docs__mark" aria-hidden="true">▢</span>
          <div class="docs__body">
            <span class="docs__name">${escapeHtml(doc)}</span>
            <label class="docs__attach">
              <input type="file" data-doc-index="${i}"
                     accept=".pdf,.jpg,.jpeg,.png,.webp,.tif,.tiff">
              <span class="docs__attach-label">Attach your file</span>
            </label>
          </div>
        </li>`
      )
      .join("");

    return `
      <ul class="docs">${items}</ul>
      <div class="pack">
        <p class="pack__note">Your own documents only. Scholar Hunter never writes a
        motivation letter, transcript or reference for you, and never submits on your
        behalf — it orders your files into one PDF for the portal.</p>
        <div class="pack__row">
          <button type="button" class="btn btn--outline" data-action="pack" disabled>
            <span class="btn__label">Build combined PDF</span>
          </button>
          <span class="pack__count" data-pack-count>0 of ${documents.length} attached</span>
        </div>
        <div class="pack__result" data-pack-result></div>
      </div>`;
  }

  /* Where the student actually applies. The URL is only ever one the page really
     carried, so this never sends anyone to an invented portal. */
  function renderApply(item) {
    const method = isStated(item.application_method)
      ? escapeHtml(item.application_method)
      : "";
    if (!isStated(item.application_url)) {
      return `<p class="muted-note">No application link was found on this page${
        method ? ` &mdash; it says applications go by ${method}` : ""
      }. Open the source above to find where to apply.</p>`;
    }
    return `
      <div class="apply__row">
        <a class="btn btn--solid" href="${escapeHtml(item.application_url)}"
           target="_blank" rel="noopener noreferrer">Open application portal \u2197</a>
        <button type="button" class="btn btn--outline" data-action="autofill">
          <span class="btn__label">Auto-fill in a browser</span>
        </button>
      </div>
      <p class="apply__url">${escapeHtml(item.application_url)}</p>
      <p class="muted-note">Auto-fill opens the portal in its own window, types what
      it can from your profile, and stops. It never ticks a declaration and never
      submits — you check the form and submit it yourself.</p>
      <div class="apply__result" data-autofill-result></div>`;
  }

  function renderGuidance(guidance) {
    if (!guidance || !guidance.length) return "";
    return guidance
      .map(
        (row) => `
        <div class="doc-guide">
          <h4 class="doc-guide__name">${escapeHtml(row.document)}</h4>
          ${row.summary ? `<p class="doc-guide__summary">${escapeHtml(row.summary)}</p>` : ""}
          <ol class="doc-guide__steps">
            ${(row.steps || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("")}
          </ol>
          ${
            row.watch_out
              ? `<p class="doc-guide__warn"><span class="doc-guide__warn-tag">WATCH OUT</span> ${escapeHtml(
                  row.watch_out
                )}</p>`
              : ""
          }
        </div>`
      )
      .join("");
  }

  /* Guidance costs a model call, so it is fetched the first time a card is
     opened — never for results the student only scrolls past. */
  async function loadDocumentHelp(card, item) {
    const section = card.querySelector("[data-doc-help]");
    if (!section || section.dataset.loaded === "yes") return;

    if (!item.documents || !item.documents.length) {
      section.hidden = true; // nothing to advise on
      return;
    }

    section.dataset.loaded = "yes";
    const body = section.querySelector(".doc-help__body");
    body.innerHTML =
      '<p class="doc-help__loading"><span class="btn__spinner" aria-hidden="true"></span> Working out how to prepare these…</p>';

    try {
      const response = await fetch("/document_help", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          opportunity: item,
          documents: item.documents,
          profile: lastProfile,
        }),
      });
      const data = await response.json();

      if (!response.ok || !data.ok) {
        body.innerHTML = `<p class="doc-help__error">${escapeHtml(
          data.error || "Could not prepare guidance for these documents."
        )}</p>`;
        section.dataset.loaded = "no"; // allow a retry on the next open
        return;
      }
      body.innerHTML =
        renderGuidance(data.guidance) ||
        '<p class="muted-note">No guidance available for these documents.</p>';
    } catch (err) {
      body.innerHTML =
        '<p class="doc-help__error">Could not reach the server for document guidance.</p>';
      section.dataset.loaded = "no";
    }
  }

  function renderCard(item, index) {
    const verdict = VERDICT_TEXT[item.verdict] || VERDICT_TEXT.unclear;
    const institution = isStated(item.institution)
      ? `<span>${escapeHtml(item.institution)}</span>`
      : "";
    const type = isStated(item.type) ? escapeHtml(item.type) : "opportunity";
    // Nothing to save when the page never stated one — say so on the button
    // rather than letting the click fail.
    const hasDeadline = isStated(item.deadline);

    const card = document.createElement("article");
    card.className = "result";
    // A small stagger so the list assembles rather than snapping in at once.
    card.style.animationDelay = `${Math.min(index, 6) * 60}ms`;

    card.innerHTML = `
      <div class="result__head">
        <h3 class="result__name">${escapeHtml(item.name)}</h3>
        <span class="badge badge--${escapeHtml(item.verdict)}">
          <span aria-hidden="true">${verdict.icon}</span> ${verdict.label}
        </span>
      </div>

      <div class="result__meta">
        <span class="type-chip">${type}</span>
        ${institution}
        ${item.trusted_source ? "<span>· trusted source</span>" : ""}
      </div>

      <!-- Always visible: enough to judge and act on without opening the file. -->
      <div class="result__section">
        <ul class="ledger facts">
          <li>
            <div class="ledger__row">
              <span class="ledger__key">Deadline</span>
              <span class="ledger__leader" aria-hidden="true"></span>
              ${renderDeadline(item)}
            </div>
          </li>
        </ul>
        <a class="source-link" href="${escapeHtml(item.url)}"
           target="_blank" rel="noopener noreferrer">${escapeHtml(item.url)}</a>
      </div>

      <!-- The full audit, opened on request so a shortlist stays scannable. -->
      <div class="result__details" hidden>
        <div class="result__section">
          <span class="result__label">Fit</span>
          <p class="result__fit">${escapeHtml(item.fit || item.verdict_reason)}</p>
        </div>

        <div class="result__section">
          <span class="result__label">Requirements &amp; eligibility</span>
          ${renderRequirements(item.requirements)}
        </div>

        <div class="result__section">
          <span class="result__label">Course match</span>
          ${renderCourseMatch(item.course_match)}
        </div>

        <div class="result__section">
          <span class="result__label">Documents to submit</span>
          ${renderDocuments(item.documents)}
        </div>

        <div class="result__section doc-help" data-doc-help>
          <span class="result__label">How to prepare these</span>
          <div class="doc-help__body"></div>
        </div>

        <div class="result__section">
          <span class="result__label">Where to apply</span>
          <div class="apply">${renderApply(item)}</div>
        </div>

        <div class="result__section">
          <span class="result__label">Funding</span>
          <ul class="ledger facts">
            <li>
              <div class="ledger__row">
                <span class="ledger__key">Covers</span>
                <span class="ledger__leader" aria-hidden="true"></span>
                <span class="ledger__val">${
                  isStated(item.funding) ? escapeHtml(item.funding) : "Not stated"
                }</span>
              </div>
            </li>
          </ul>
        </div>
      </div>

      <button type="button" class="toggle" aria-expanded="false">
        <span class="toggle__label">View more</span>
        <span class="toggle__arrow" aria-hidden="true">⌄</span>
      </button>

      <div class="actions">
        <button type="button" class="btn btn--outline" data-action="draft">
          <span class="btn__label">Draft email</span>
        </button>
        <button type="button" class="btn btn--outline" data-action="deadline"
          ${hasDeadline ? "" : 'disabled title="This page states no deadline, so there is nothing to save."'}>
          <span class="btn__label">Save deadline</span>
        </button>
      </div>`;

    // Attaching documents: enable the build button and keep a running count.
    const packBtn = card.querySelector('[data-action="pack"]');
    if (packBtn) {
      const inputs = Array.from(card.querySelectorAll("input[data-doc-index]"));
      const countEl = card.querySelector("[data-pack-count]");
      const refresh = () => {
        const attached = inputs.filter((i) => i.files && i.files.length).length;
        countEl.textContent = `${attached} of ${inputs.length} attached`;
        packBtn.disabled = attached === 0;
        inputs.forEach((input) => {
          const label = input.parentElement.querySelector(".docs__attach-label");
          const file = input.files && input.files[0];
          label.textContent = file ? file.name : "Attach your file";
          input.closest("li").classList.toggle("is-attached", Boolean(file));
        });
      };
      inputs.forEach((input) => input.addEventListener("change", refresh));
      packBtn.addEventListener("click", () => buildPack(card, item, inputs, packBtn));
    }

    const autofillBtn = card.querySelector('[data-action="autofill"]');
    if (autofillBtn) {
      autofillBtn.addEventListener("click", () => runAutofill(card, item, autofillBtn));
    }

    // Expand / collapse. Document guidance loads the first time it opens.
    const toggle = card.querySelector(".toggle");
    const details = card.querySelector(".result__details");
    toggle.addEventListener("click", () => {
      const opening = details.hidden;
      details.hidden = !opening;
      toggle.setAttribute("aria-expanded", String(opening));
      toggle.classList.toggle("is-open", opening);
      toggle.querySelector(".toggle__label").textContent = opening
        ? "View less"
        : "View more";
      if (opening) loadDocumentHelp(card, item);
    });

    card
      .querySelector('[data-action="draft"]')
      .addEventListener("click", (event) => onDraftEmail(event.currentTarget, item, card));

    const deadlineBtn = card.querySelector('[data-action="deadline"]');
    if (hasDeadline) {
      deadlineBtn.addEventListener("click", (event) =>
        onSaveDeadline(event.currentTarget, item, card)
      );
    }

    return card;
  }

  /* Send the student's own files to be merged, then hand the PDF straight back
     as a download. Nothing is stored and nothing is submitted — the student
     reviews the pack and uploads it to the portal themselves. */
  async function buildPack(card, item, inputs, button) {
    const resultEl = card.querySelector("[data-pack-result]");
    const original = button.querySelector(".btn__label").textContent;
    button.disabled = true;
    button.innerHTML =
      '<span class="btn__spinner" aria-hidden="true"></span>' +
      '<span class="btn__label">Building…</span>';
    resultEl.innerHTML = "";

    const data = new FormData();
    data.append("documents", JSON.stringify(item.documents || []));
    data.append("opportunity", item.name || "");
    data.append("institution", item.institution || "");
    inputs.forEach((input) => {
      if (input.files && input.files[0]) {
        data.append(`doc_${input.dataset.docIndex}`, input.files[0]);
      }
    });

    try {
      const response = await fetch("/application_pack", { method: "POST", body: data });

      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        resultEl.innerHTML = `<p class="pack__error">${escapeHtml(
          body.error || "The pack could not be built."
        )}</p>`;
        return;
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "application-pack.pdf";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);

      const pages = response.headers.get("X-Pack-Pages") || "?";
      const included = response.headers.get("X-Pack-Included") || "?";
      let skipped = [], missing = [];
      try { skipped = JSON.parse(response.headers.get("X-Pack-Skipped") || "[]"); } catch (e) {}
      try { missing = JSON.parse(response.headers.get("X-Pack-Missing") || "[]"); } catch (e) {}

      let html = `<p class="pack__ok">&#10003; Combined PDF downloaded &mdash; ${escapeHtml(
        included
      )} document(s), ${escapeHtml(pages)} pages.</p>`;

      // Say what is not in the pack. Discovering a gap after submitting is the
      // failure this feature exists to prevent.
      if (skipped.length) {
        html += `<p class="pack__warn"><strong>Left out:</strong> ${skipped
          .map((s) => `${escapeHtml(s.filename)} &mdash; ${escapeHtml(s.why)}`)
          .join("; ")}</p>`;
      }
      if (missing.length) {
        // Requirement names run long, so show a count and shortened titles
        // rather than eight full sentences of small amber text.
        const shown = missing
          .slice(0, 4)
          .map((m) => escapeHtml(m.length > 58 ? m.slice(0, 58).trimEnd() + "…" : m))
          .map((m) => `<li>${m}</li>`)
          .join("");
        const more = missing.length > 4 ? `<li>and ${missing.length - 4} more</li>` : "";
        html += `<div class="pack__warn"><strong>Still missing ${missing.length} of ${
          (item.documents || []).length
        }:</strong><ul class="pack__missing">${shown}${more}</ul></div>`;
      }
      html += '<p class="pack__note">Review it before you upload it to the portal.</p>';
      resultEl.innerHTML = html;
    } catch (err) {
      resultEl.innerHTML =
        '<p class="pack__error">Could not reach the server to build the pack.</p>';
    } finally {
      button.disabled = false;
      button.innerHTML = `<span class="btn__label">${escapeHtml(original)}</span>`;
    }
  }

  /* Open the portal in a real browser and fill what we can. Two deliberate
     steps, because the student usually has to log in between them — and because
     nothing should happen to an application form without them watching. */
  async function runAutofill(card, item, button) {
    const resultEl = card.querySelector("[data-autofill-result]");
    const original = button.querySelector(".btn__label").textContent;
    const busy = (label) => {
      button.disabled = true;
      button.innerHTML =
        '<span class="btn__spinner" aria-hidden="true"></span>' +
        `<span class="btn__label">${escapeHtml(label)}</span>`;
    };
    const restore = () => {
      button.disabled = false;
      button.innerHTML = `<span class="btn__label">${escapeHtml(original)}</span>`;
    };

    busy("Opening…");
    resultEl.innerHTML = "";

    try {
      const opened = await fetch("/autofill/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: item.application_url }),
      });
      const openBody = await opened.json();
      if (!opened.ok || !openBody.ok) {
        resultEl.innerHTML = `<p class="pack__error">${escapeHtml(
          openBody.error || "Could not open the portal."
        )}</p>`;
        return;
      }

      resultEl.innerHTML =
        '<p class="muted-note">Portal open in a separate window. Log in and reach the ' +
        'application form if you need to, then filling starts in a moment…</p>';
      // Give the student a beat to land on the form before typing into it.
      await new Promise((r) => setTimeout(r, 4000));

      busy("Filling…");
      const filled = await fetch("/autofill/fill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: lastProfile }),
      });
      const body = await filled.json();
      if (!filled.ok || !body.ok) {
        resultEl.innerHTML = `<p class="pack__error">${escapeHtml(
          body.error || "Could not fill the form."
        )}</p>`;
        return;
      }

      const fields = body.filled || [];
      if (!fields.length) {
        resultEl.innerHTML =
          '<p class="pack__warn">No fields matched your profile on this page. If you ' +
          'were still logging in, reach the application form and press Auto-fill again.</p>';
        return;
      }

      resultEl.innerHTML =
        `<p class="pack__ok">&#10003; Filled ${fields.length} field${
          fields.length === 1 ? "" : "s"
        } &mdash; nothing submitted.</p>` +
        `<ul class="autofill__list">${fields
          .map((f) => `<li><span>${escapeHtml(f.field)}</span><em>${escapeHtml(f.value)}</em></li>`)
          .join("")}</ul>` +
        '<p class="muted-note">Check every field, complete the rest yourself, then ' +
        'submit it in that window.</p>';
    } catch (err) {
      resultEl.innerHTML =
        '<p class="pack__error">Could not reach the server to drive the browser.</p>';
    } finally {
      restore();
    }
  }

  // --- card actions ------------------------------------------------------

  function setBusy(button, busyLabel) {
    button.disabled = true;
    button.dataset.original = button.querySelector(".btn__label").textContent;
    button.innerHTML =
      '<span class="btn__spinner" aria-hidden="true"></span>' +
      `<span class="btn__label">${escapeHtml(busyLabel)}</span>`;
  }

  function setDone(button, label) {
    button.classList.add("is-done");
    button.innerHTML = `<span class="btn__label">✓ ${escapeHtml(label)}</span>`;
    // Return to normal after a few seconds so the action stays repeatable.
    setTimeout(() => {
      button.classList.remove("is-done");
      button.disabled = false;
      button.innerHTML = `<span class="btn__label">${escapeHtml(
        button.dataset.original || label
      )}</span>`;
    }, 4000);
  }

  function setFailed(button, card, message) {
    button.disabled = false;
    button.innerHTML = `<span class="btn__label">${escapeHtml(
      button.dataset.original || "Try again"
    )}</span>`;
    showCardNote(card, message);
  }

  /* Card-level problems belong on the card, not in a browser alert and not in
     the page-wide banner — the student needs to know which result it refers to. */
  function showCardNote(card, message) {
    let note = card.querySelector(".action-note");
    if (!note) {
      note = document.createElement("p");
      note.className = "action-note";
      note.setAttribute("role", "status");
      card.querySelector(".actions").appendChild(note);
    }
    note.textContent = message;
  }

  async function onDraftEmail(button, item, card) {
    if (!gmailReady) {
      showCardNote(
        card,
        "Email drafting is not configured. Add a Google OAuth credentials.json to the project root to enable it — everything else works without it."
      );
      return;
    }

    const to = window.prompt(
      "Who should this inquiry go to?\n(The programme's admissions or scholarship office.)",
      ""
    );
    if (!to) return; // cancelled — nothing is drafted without a deliberate click

    setBusy(button, "Drafting…");
    try {
      const response = await fetch("/draft_email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to: to, opportunity: item }),
      });
      const body = await response.json();
      if (!response.ok || !body.ok) {
        setFailed(button, card, body.error || "The draft could not be created.");
        return;
      }
      setDone(button, "Draft created");
      showCardNote(
        card,
        "Saved to your Gmail Drafts. Nothing has been sent — review and send it yourself."
      );
    } catch (err) {
      setFailed(button, card, "Could not reach the server. Is it still running?");
    }
  }

  async function onSaveDeadline(button, item, card) {
    setBusy(button, "Saving…");
    try {
      const response = await fetch("/save_deadline", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ opportunity: item }),
      });
      const body = await response.json();
      if (!response.ok || !body.ok) {
        setFailed(button, card, body.error || "The deadline could not be saved.");
        return;
      }
      setDone(button, "Deadline saved");
    } catch (err) {
      setFailed(button, card, "Could not reach the server. Is it still running?");
    }
  }

  // --- tabs --------------------------------------------------------------

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((other) => {
        const active = other === tab;
        other.classList.toggle("is-active", active);
        other.setAttribute("aria-selected", String(active));
      });
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("is-active", panel.id === `panel-${tab.dataset.tab}`);
      });
    });
  });

  // --- submit ------------------------------------------------------------

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();

    const missing = ["field_of_study", "degree_level", "nationality"].filter(
      (name) => !form.elements[name].value.trim()
    );
    if (missing.length) {
      showError(
        "Please fill in the required fields: field of study, degree level, and country."
      );
      form.elements[missing[0]].focus();
      return;
    }

    // Remember the profile so expanded cards can tailor document guidance to it.
    lastProfile = {
      field_of_study: form.elements.field_of_study.value.trim(),
      degree_level: form.elements.degree_level.value.trim(),
      gpa: form.elements.gpa.value.trim(),
      nationality: form.elements.nationality.value.trim(),
      interests: form.elements.interests.value.trim(),
      // Identity fields, used only to fill a portal's form.
      full_name: form.elements.full_name.value.trim(),
      email: form.elements.email.value.trim(),
      university: form.elements.university.value.trim(),
      courses: form.elements.courses.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
    };

    resultsEl.innerHTML = "";
    summaryEl.hidden = true;
    profileStrip.textContent = profileSummary();
    showResultsView();
    loading.hidden = false;
    startLoadingNarration();
    submitBtn.disabled = true;
    submitBtn.innerHTML =
      '<span class="btn__spinner" aria-hidden="true"></span>' +
      '<span class="btn__label">Searching…</span>';

    let failed = false;
    try {
      // Send as multipart so a transcript upload rides along with the profile.
      const response = await fetch("/search", {
        method: "POST",
        body: new FormData(form),
      });
      const body = await response.json();

      if (!response.ok || !body.ok) {
        failed = true;
        showError(body.error || "The search could not be completed.");
        return;
      }
      hasShortlist = true;
      renderResults(body);
    } catch (err) {
      failed = true;
      showError(
        "Could not reach the server. Check that `python app.py` is still running in your terminal."
      );
    } finally {
      stopLoadingNarration();
      loading.hidden = true;
      submitBtn.disabled = false;
      submitBtn.innerHTML = '<span class="btn__label">Find scholarships</span>';
      // A failed search returns to the form so the student can fix and retry.
      if (failed) showProfileView();
    }
  });

  function renderResults(body) {
    const items = body.results || [];

    if (!items.length) {
      summaryEl.hidden = true;
      resultsEl.innerHTML = `
        <div class="card empty">
          <h3>No opportunities you can apply to yet</h3>
          <p>${escapeHtml(
            body.message ||
              "Nothing matched this profile. Nothing has been invented to fill the gap — try broadening your field or interests."
          )}</p>
        </div>`;
      return;
    }

    summaryEl.hidden = false;
    summaryEl.textContent =
      `${items.length} opportunit${items.length === 1 ? "y" : "ies"} you can apply to` +
      (body.considered ? `, from ${body.considered} pages checked. ` : ". ") +
      (body.message || "");

    const fragment = document.createDocumentFragment();
    items.forEach((item, index) => fragment.appendChild(renderCard(item, index)));
    resultsEl.appendChild(fragment);
    summaryEl.scrollIntoView({ behavior: "smooth", block: "start" });
  }
})();

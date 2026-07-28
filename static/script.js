/* Scholar Hunter — frontend logic. Plain JS, no framework, no build step. */

(function () {
  "use strict";

  const form = document.getElementById("profile-form");
  const submitBtn = document.getElementById("submit-btn");
  const loading = document.getElementById("loading");
  const resultsEl = document.getElementById("results");
  const summaryEl = document.getElementById("results-summary");
  const errorBanner = document.getElementById("error-banner");
  const errorText = document.getElementById("error-text");

  // Whether Gmail drafting is configured. Checked once so the Draft button can
  // explain itself instead of failing on click.
  let gmailReady = true;

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
      .map(
        (row) => `
        <li>
          <span class="reqs__mark reqs__mark--${escapeHtml(row.status)}" aria-hidden="true">${
            STATUS_MARK[row.status] || "–"
          }</span>
          <span>
            <span class="reqs__label">${escapeHtml(row.label)}:</span>
            <span class="reqs__value">${escapeHtml(row.required)}</span>
            <span class="reqs__status">— ${escapeHtml(
              STATUS_WORD[row.status] || row.status
            )}${row.note ? ". " + escapeHtml(row.note) : ""}</span>
          </span>
        </li>`
      )
      .join("");
    return `<ul class="reqs">${items}</ul>`;
  }

  function renderCourseMatch(match) {
    if (!match || !match.assessed) {
      return `<p class="course-match" style="margin:0">Not assessed${
        match && match.summary === "not assessed"
          ? " — add your courses above, or this programme lists no prerequisites."
          : "."
      }</p>`;
    }
    const pct = match.total ? Math.round((match.matched / match.total) * 100) : 0;
    const note = match.confidence_capped
      ? '<span class="course-match__note"> Confidence is capped: course names were given without descriptions.</span>'
      : "";
    return `
      <p class="course-match" style="margin:0">
        <strong>${escapeHtml(match.summary)}</strong>${note}
      </p>
      <div class="bar" role="img" aria-label="${escapeHtml(match.summary)}">
        <div class="bar__fill" style="width:${pct}%"></div>
      </div>`;
  }

  function renderDeadline(item) {
    if (!isStated(item.deadline)) {
      return "<span>Not stated</span>";
    }
    const text = escapeHtml(item.deadline);
    const days = daysUntil(item.deadline);

    if (item.deadline_status === "expired" || (days !== null && days < 0)) {
      return `<span class="deadline--expired">${text} — closed</span>`;
    }
    if (days !== null && days <= 30) {
      return `<span class="deadline--soon">${text} — ${days} day${
        days === 1 ? "" : "s"
      } left</span>`;
    }
    return `<span>${text}</span>`;
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
        <span class="result__label">Deadline &amp; funding</span>
        <div class="facts">
          <div><strong>Deadline:</strong> ${renderDeadline(item)}</div>
          <div><strong>Funding:</strong> ${
            isStated(item.funding) ? escapeHtml(item.funding) : "Not stated"
          }</div>
        </div>
      </div>

      <div class="result__section">
        <span class="result__label">Source</span>
        <a class="source-link" href="${escapeHtml(item.url)}"
           target="_blank" rel="noopener noreferrer">${escapeHtml(item.url)}</a>
      </div>

      <div class="actions">
        <button type="button" class="btn btn--outline" data-action="draft">
          <span class="btn__label">Draft email</span>
        </button>
        <button type="button" class="btn btn--outline" data-action="deadline"
          ${hasDeadline ? "" : 'disabled title="This page states no deadline, so there is nothing to save."'}>
          <span class="btn__label">Save deadline</span>
        </button>
      </div>`;

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

    resultsEl.innerHTML = "";
    summaryEl.hidden = true;
    loading.hidden = false;
    submitBtn.disabled = true;
    submitBtn.innerHTML =
      '<span class="btn__spinner" aria-hidden="true"></span>' +
      '<span class="btn__label">Searching…</span>';

    try {
      // Send as multipart so a transcript upload rides along with the profile.
      const response = await fetch("/search", {
        method: "POST",
        body: new FormData(form),
      });
      const body = await response.json();

      if (!response.ok || !body.ok) {
        showError(body.error || "The search could not be completed.");
        return;
      }
      renderResults(body);
    } catch (err) {
      showError(
        "Could not reach the server. Check that `python app.py` is still running in your terminal."
      );
    } finally {
      loading.hidden = true;
      submitBtn.disabled = false;
      submitBtn.innerHTML = '<span class="btn__label">Find Scholarships</span>';
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

// Prevent accidental double submission.
// Any <form data-submit-once> disables its submit button after the first
// click, so pressing "Add product" twice can't send two requests.
document.addEventListener("submit", function (event) {
  const form = event.target;
  if (!form.hasAttribute("data-submit-once")) return;

  if (form.dataset.submitting === "true") {
    event.preventDefault(); // already sending -- ignore the second click
    return;
  }
  form.dataset.submitting = "true";
  form.querySelectorAll("button[type=submit]").forEach(function (button) {
    button.disabled = true;
    if (button.dataset.busyText) button.textContent = button.dataset.busyText;
  });
});

// If the user comes back with the browser's Back button, the page may be
// restored from cache with the button still disabled. Reload it fresh.
window.addEventListener("pageshow", function (event) {
  if (event.persisted && document.querySelector("form[data-submit-once]")) {
    window.location.reload();
  }
});

// ---------------------------------------------------------------------------
// Bulk selection (inventory page and manager request queue)
//
// <section data-bulk> contains:
//   [data-bulk-start]    button that turns on edit mode (optional)
//   [data-bulk-toolbar]  toolbar shown while selecting
//   [data-select-all]    header checkbox: ticks every VISIBLE row checkbox
//   [data-select-item]   one checkbox per row
//   [data-selected-count]  where "N" selected is written
//   [data-bulk-action]   buttons that need at least one row selected
//   [data-bulk-cancel]   leaves edit mode and clears the selection
// Add data-bulk-always to show checkboxes all the time (no edit mode).
//
// "Visible" means the rows the server sent for the current search/filter,
// so Select All never picks hidden products.
// ---------------------------------------------------------------------------
function setupBulkSelection(section) {
  const selectAll = section.querySelector("[data-select-all]");
  const toolbar = section.querySelector("[data-bulk-toolbar]");
  const startButton = section.querySelector("[data-bulk-start]");
  const cancelButton = section.querySelector("[data-bulk-cancel]");
  const items = function () {
    return Array.from(section.querySelectorAll("[data-select-item]"));
  };

  function update() {
    const all = items();
    const selected = all.filter(function (box) { return box.checked; }).length;
    section.querySelectorAll("[data-selected-count]").forEach(function (el) {
      el.textContent = selected;
    });
    section.querySelectorAll("[data-bulk-action]").forEach(function (button) {
      button.disabled = selected === 0;
    });
    if (selectAll) {
      selectAll.checked = all.length > 0 && selected === all.length;
      selectAll.indeterminate = selected > 0 && selected < all.length;
    }
    // Highlight selected rows
    all.forEach(function (box) {
      const row = box.closest("tr");
      if (row) row.classList.toggle("is-selected", box.checked);
    });
  }

  function setEditing(on) {
    section.classList.toggle("is-editing", on);
    if (toolbar) toolbar.hidden = !on;
    if (startButton) startButton.hidden = on;
    if (!on) {
      items().forEach(function (box) { box.checked = false; }); // clear selection
    }
    update();
  }

  if (selectAll) {
    selectAll.addEventListener("change", function () {
      items().forEach(function (box) { box.checked = selectAll.checked; });
      update();
    });
  }
  section.addEventListener("change", function (event) {
    if (event.target.matches("[data-select-item]")) update();
  });
  if (startButton) startButton.addEventListener("click", function () { setEditing(true); });
  if (cancelButton) cancelButton.addEventListener("click", function () { setEditing(false); });

  setEditing(section.hasAttribute("data-bulk-always"));
}

document.querySelectorAll("[data-bulk]").forEach(setupBulkSelection);

// Ask before submitting buttons marked data-confirm="Are you sure ...?"
document.addEventListener("click", function (event) {
  const button = event.target.closest("[data-confirm]");
  if (button && !window.confirm(button.dataset.confirm)) {
    event.preventDefault();
  }
});

// ---------------------------------------------------------------------------
// Employee "Report a problem" form: show only the fields the chosen request
// type needs. Hidden fields are DISABLED too, so the browser doesn't require
// or send them. (The server checks every type itself; this is just for ease.)
// ---------------------------------------------------------------------------
document.querySelectorAll("[data-report-form]").forEach(function (form) {
  const typeSelect = form.querySelector("[data-report-type]");
  const productSelect = form.querySelector("[data-report-product]");
  const recorded = form.querySelector("[data-recorded-quantity]");
  const direction = form.querySelector("[data-adjust-direction]");
  const reason = form.querySelector("[data-adjust-reason]");
  const quantityLabel = form.querySelector("[data-quantity-label]");
  const notesLabel = form.querySelector("[data-notes-label]");

  function update() {
    const type = typeSelect.value;

    form.querySelectorAll("[data-show-for]").forEach(function (group) {
      const show = group.dataset.showFor.split(" ").indexOf(type) !== -1;
      group.hidden = !show;
      group.querySelectorAll("input, select, textarea").forEach(function (field) {
        field.disabled = !show;
      });
    });

    quantityLabel.textContent = type === "restock" ? "Quantity to order" : "Quantity";
    notesLabel.textContent = type === "restock" ? "Reason" : "Explanation";

    // Only offer reasons that match Increase / Decrease.
    if (direction && reason) {
      let firstMatch = null;
      Array.prototype.forEach.call(reason.options, function (option) {
        const matches = option.dataset.direction === direction.value;
        option.hidden = !matches;
        option.disabled = !matches;
        if (matches && !firstMatch) firstMatch = option;
      });
      if (reason.selectedOptions[0] && reason.selectedOptions[0].disabled && firstMatch) {
        firstMatch.selected = true;
      }
    }

    // Show the currently recorded quantity (helps with counts and decreases).
    const chosen = productSelect.selectedOptions[0];
    if (chosen && chosen.value) {
      recorded.hidden = false;
      recorded.textContent = "Currently recorded: " + chosen.dataset.quantity + " " + chosen.dataset.unit + ".";
    } else {
      recorded.hidden = true;
    }
  }

  typeSelect.addEventListener("change", update);
  productSelect.addEventListener("change", update);
  if (direction) direction.addEventListener("change", update);
  update();
});

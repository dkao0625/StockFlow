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

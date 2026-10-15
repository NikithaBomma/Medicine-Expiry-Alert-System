document.querySelectorAll(".password-visibility").forEach((button) => {
  button.addEventListener("click", () => {
    const password = button.previousElementSibling;
    const visible = password.type === "password";
    password.type = visible ? "text" : "password";
    button.textContent = visible ? "Hide" : "Show";
    button.setAttribute("aria-label", `${visible ? "Hide" : "Show"} password`);
    button.setAttribute("aria-pressed", String(visible));
  });
});

document.querySelectorAll(".auth-form").forEach((form) => {
  const confirmation = form.elements.confirm_password;
  if (confirmation) {
    confirmation.addEventListener("input", () => confirmation.setCustomValidity(""));
  }
  form.addEventListener("submit", (event) => {
    const password = form.elements.password;
    if (confirmation && password.value !== confirmation.value) {
      event.preventDefault();
      confirmation.setCustomValidity("Passwords do not match.");
      confirmation.reportValidity();
    }
  });
});

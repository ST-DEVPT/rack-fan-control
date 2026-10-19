const form = $("#form"), btn = $("#submit"), err = $("#error");
form.onsubmit = async e => {
  e.preventDefault();
  btn.disabled = true; btn.textContent = "Signing in…"; err.textContent = "";
  try {
    const r = await api("/api/login", { password: form.password.value, remember: form.remember.checked });
    if (r.ok) return location.replace("/");
    err.textContent = await failure(r);
    form.password.select();
  } catch {
    err.textContent = "Cannot reach the dashboard";
  }
  btn.disabled = false; btn.textContent = "Sign in";
};

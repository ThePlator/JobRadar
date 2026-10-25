// The sign-in page: Firebase signs you in (Google, email and password, or a text message), then
// the server swaps that sign-in for its own session cookie. Firebase keeps nothing in the
// browser afterwards (in-memory persistence): the cookie is the only sign-in that lasts.
const VERSION = "13.1.0";
const BASE = `https://www.gstatic.com/firebasejs/${VERSION}/`;
const { initializeApp } = await import(`${BASE}firebase-app.js`);
const fb = await import(`${BASE}firebase-auth.js`);

const box = document.querySelector("[data-signin]");
const msg = box.querySelector("[data-msg]");
const auth = fb.getAuth(initializeApp({
  apiKey: box.dataset.apiKey, authDomain: box.dataset.authDomain, projectId: box.dataset.projectId,
}));
auth.useDeviceLanguage();
await fb.setPersistence(auth, fb.inMemoryPersistence);

const ERRORS = {
  "auth/invalid-credential": "That email and password don't match. Try again, or reset your password.",
  "auth/invalid-login-credentials": "That email and password don't match. Try again, or reset your password.",
  "auth/user-not-found": "There's no account with that email. Create one instead.",
  "auth/wrong-password": "That password isn't right. Try again, or reset it.",
  "auth/email-already-in-use": "There's already an account with that email. Sign in instead.",
  "auth/weak-password": "Use a password of at least 6 characters.",
  "auth/invalid-email": "That email address doesn't look right.",
  "auth/missing-password": "Write your password first.",
  "auth/popup-blocked": "Your browser blocked Google's window. Allow pop-ups for this site and try again.",
  "auth/popup-closed-by-user": "",
  "auth/cancelled-popup-request": "",
  "auth/invalid-phone-number": "Write the number with its country code, like +91 98765 43210.",
  "auth/missing-phone-number": "Write your phone number first.",
  "auth/invalid-verification-code": "That code isn't right. Check the text message and try again.",
  "auth/code-expired": "That code has expired. Send a new one.",
  "auth/too-many-requests": "Too many tries. Wait a few minutes, then try again.",
  "auth/quota-exceeded": "Too many codes were sent today. Use Google or email for now.",
  "auth/billing-not-enabled": "Phone sign-in isn't set up yet. Use Google or email for now.",
  "auth/network-request-failed": "Couldn't reach the sign-in service. Check your connection and try again.",
  "auth/unauthorized-domain": "This address can't sign in yet: add it in Firebase under Authentication, Settings, Authorized domains.",
  "auth/operation-not-allowed": "This way of signing in is switched off. Use another one.",
  "auth/account-exists-with-different-credential": "You already have an account with this email. Sign in the way you did before.",
};

const say = (text, good = false) => {
  msg.textContent = text;
  msg.classList.toggle("good", good);
  msg.hidden = !text;
};
const busy = (on) => box.querySelectorAll("button:not(#theme), input").forEach((el) => { el.disabled = on; });
const explain = (e) => {
  if (e && e.code) return e.code in ERRORS ? ERRORS[e.code] : "Signing in didn't work. Try again.";
  return (e && e.message) || "Signing in didn't work. Try again.";
};
const run = async (work) => {
  say("");
  busy(true);
  try {
    await work();
  } catch (e) {
    say(explain(e));
    busy(false);
  }
};

// Swap Firebase's sign-in for JobRadar's session, then go where you were heading.
const finish = async (cred) => {
  const idToken = await cred.user.getIdToken();
  const res = await fetch("/api/v1/session", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-jobradar-csrf": box.dataset.csrf },
    body: JSON.stringify({ id_token: idToken, next: box.dataset.next }),
  });
  const data = await res.json().catch(() => ({}));
  await fb.signOut(auth);
  if (!res.ok) throw new Error(data.message || "Signing in didn't work. Reload the page and try again.");
  location.assign(data.next || "/");
};

// ---- Google ----
box.querySelector("[data-google]")?.addEventListener("click", () =>
  run(async () => finish(await fb.signInWithPopup(auth, new fb.GoogleAuthProvider()))));

// ---- Email or phone ----
box.querySelectorAll("[data-tab]").forEach((tab) => tab.addEventListener("click", () => {
  box.querySelectorAll("[data-tab]").forEach((t) => t.setAttribute("aria-pressed", String(t === tab)));
  box.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
  say("");
}));

const emailForm = box.querySelector("[data-email-form]");
emailForm?.addEventListener("submit", (e) => {
  e.preventDefault();
  const email = emailForm.email.value.trim();
  const password = emailForm.password.value;
  const create = e.submitter?.value === "create";
  run(async () => {
    if (create) {
      const cred = await fb.createUserWithEmailAndPassword(auth, email, password);
      await fb.sendEmailVerification(cred.user);
      await finish(cred);
    } else {
      await finish(await fb.signInWithEmailAndPassword(auth, email, password));
    }
  });
});
box.querySelector("[data-reset]")?.addEventListener("click", () => {
  const email = emailForm.email.value.trim();
  if (!email) {
    say("Write your email first, then ask for a reset link.");
    emailForm.email.focus();
    return;
  }
  run(async () => {
    await fb.sendPasswordResetEmail(auth, email);
    busy(false);
    say(`If there's an account for ${email}, a reset link is on its way. Check your inbox.`, true);
  });
});

const phoneForm = box.querySelector("[data-phone-form]");
let verifier = null;
let pending = null;
phoneForm?.addEventListener("submit", (e) => {
  e.preventDefault();
  const number = phoneForm.phone.value.replace(/[\s()-]/g, "");
  run(async () => {
    verifier ??= new fb.RecaptchaVerifier(auth, "recaptcha", { size: "invisible" });
    try {
      pending = await fb.signInWithPhoneNumber(auth, number, verifier);
    } catch (err) {
      verifier.clear();
      verifier = null;
      throw err;
    }
    busy(false);
    phoneForm.querySelector("[data-code]").hidden = false;
    phoneForm.code.focus();
    say(`We sent a code to ${number}.`, true);
  });
});
box.querySelector("[data-verify]")?.addEventListener("click", () => {
  const code = phoneForm.code.value.trim();
  if (!pending || !/^\d{6}$/.test(code)) {
    say("Write the 6-digit code from the text message.");
    return;
  }
  run(async () => finish(await pending.confirm(code)));
});

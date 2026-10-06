const copy = document.getElementById('invitation-copy');
if (copy) copy.value = document.getElementById('invitation-link').href;
const secret = document.getElementById('invitation-secret');
if (secret && window.location.hash) {
    secret.value = window.location.hash.slice(1);
    window.history.replaceState(null, '', window.location.pathname);
}

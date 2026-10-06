const channels = document.querySelector('#channels');
function renumber() {
  channels.querySelectorAll('.channel').forEach((row, index) => {
    row.querySelector('[name="primary"]').value = String(index);
  });
}
document.querySelector('#add-channel').addEventListener('click', () => {
  const row = channels.querySelector('.channel').cloneNode(true);
  row.querySelector('[name="account"]').value = '';
  row.querySelector('[name="primary"]').checked = false;
  channels.append(row);
  renumber();
});
channels.addEventListener('click', event => {
  if (event.target.classList.contains('remove-channel') && channels.children.length > 1) {
    event.target.closest('.channel').remove();
    renumber();
  }
});

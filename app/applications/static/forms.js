const channels = document.querySelector('#channels');
function renumber() {
  channels.querySelectorAll('.channel').forEach((row, index) => {
    row.querySelector('[name="primary"]').value = String(index);
    row.querySelector('.channel-number').textContent = `Channel ${index + 1}`;
    row.querySelector('.remove-channel').disabled = channels.children.length === 1;
  });
}
document.querySelector('#add-channel').addEventListener('click', () => {
  const row = channels.querySelector('.channel').cloneNode(true);
  row.querySelector('[name="account"]').value = '';
  row.querySelector('[name="primary"]').checked = false;
  channels.append(row);
  renumber();
  row.querySelector('[name="account"]').focus();
});
channels.addEventListener('click', event => {
  if (event.target.classList.contains('remove-channel') && channels.children.length > 1) {
    event.target.closest('.channel').remove();
    renumber();
    if (!channels.querySelector('[name="primary"]:checked')) {
      channels.querySelector('[name="primary"]').checked = true;
      document.querySelector('#channel-feedback').textContent = 'The first remaining channel is now primary. Check your badge link before saving.';
    }
  }
});

const videoLinks = document.querySelector('#video-links');
if (videoLinks) {
  const addVideo = document.querySelector('#add-video');
  const feedback = document.querySelector('#video-feedback');
  function numberVideos() {
    videoLinks.querySelectorAll('.video-link').forEach((row, index) => {
      const input = row.querySelector('input');
      const error = row.querySelector('.field-error');
      input.id = `video-${index}`;
      row.querySelector('label').htmlFor = input.id;
      row.querySelector('.video-number').textContent = `Convention video link ${index + 1}`;
      row.querySelector('.remove-video').setAttribute('aria-label', `Remove convention video link ${index + 1}`);
      if (error) error.id = `video-error-${index}`;
      input.setAttribute('aria-describedby', `videos-help${error ? ` ${error.id}` : ''}`);
    });
    addVideo.disabled = videoLinks.children.length >= 10;
  }
  addVideo.addEventListener('click', () => {
    if (videoLinks.children.length >= 10) return;
    const row = document.querySelector('#video-row-template').content.firstElementChild.cloneNode(true);
    // Template errors are not copied into a newly added, empty link.
    row.querySelector('.field-error')?.remove();
    row.querySelector('input').removeAttribute('aria-invalid');
    videoLinks.append(row);
    numberVideos();
    row.querySelector('input').focus();
    feedback.textContent = `${videoLinks.children.length} of 10 link fields.`;
  });
  videoLinks.addEventListener('click', event => {
    if (!event.target.classList.contains('remove-video')) return;
    event.target.closest('.video-link').remove();
    numberVideos();
    addVideo.focus();
    feedback.textContent = 'Link removed from this form. Save changes to apply the removal.';
  });
  numberVideos();
}

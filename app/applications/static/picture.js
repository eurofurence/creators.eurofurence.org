const picture = document.querySelector('input[name="picture"]');
if (picture) {
  picture.addEventListener('change', () => {
    document.querySelector('#selected-picture').textContent = picture.files.length
      ? picture.files[0].name
      : 'No file selected';
  });
}

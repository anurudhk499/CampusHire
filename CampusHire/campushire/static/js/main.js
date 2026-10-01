// Dynamic "placement rounds" rows on the drive form (embedded documents)
document.addEventListener('DOMContentLoaded', function () {
  var list = document.getElementById('rounds');
  var tpl = document.getElementById('round-template');
  var add = document.getElementById('add-round');
  if (list && tpl && add) {
    add.addEventListener('click', function () { list.appendChild(tpl.content.cloneNode(true)); });
    list.addEventListener('click', function (e) {
      var btn = e.target.closest('.remove-round');
      if (btn) { btn.closest('.round-row').remove(); }
    });
    if (!list.children.length) { add.click(); }
  }
  // Confirm before destructive actions
  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!window.confirm(f.dataset.confirm)) { e.preventDefault(); } });
  });
  // Live preview for selected image files
  document.querySelectorAll('input[type=file][data-preview]').forEach(function (inp) {
    inp.addEventListener('change', function () {
      var target = document.getElementById(inp.dataset.preview);
      if (target && inp.files[0]) { target.src = URL.createObjectURL(inp.files[0]); target.classList.remove('d-none'); }
    });
  });
});

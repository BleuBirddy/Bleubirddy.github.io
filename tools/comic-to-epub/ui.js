import { convert } from "./app.js";

const $ = (id) => document.getElementById(id);
const drop = $("drop"), input = $("file"), jobs = $("jobs");
let queue = Promise.resolve();

function addJob(file) {
  const li = document.createElement("li");
  li.className = "job";
  li.innerHTML = `<div class="job-head"><span class="job-name"></span><span class="job-state">Waiting</span></div>
    <div class="bar"><div class="bar-fill"></div></div><div class="job-out"></div>`;
  li.querySelector(".job-name").textContent = file.name;
  jobs.prepend(li);
  const state = li.querySelector(".job-state"), fill = li.querySelector(".bar-fill"), out = li.querySelector(".job-out");
  const opts = { clean: $("clean").checked, split: $("split").checked, rtl: $("rtl").checked, maxQuality: $("maxq").checked };
  queue = queue.then(async () => {
    li.classList.add("running");
    try {
      const res = await convert(file, opts, ({ phase, done, total }) => {
        state.textContent = total > 1 ? `${phase} · ${done}/${total}` : phase;
        fill.style.width = `${Math.round((100 * done) / Math.max(1, total))}%`;
      });
      fill.style.width = "100%";
      li.classList.replace("running", "ok");
      state.textContent = `${res.pages} pages` + (opts.clean ? ` · ${res.cleaned} text blocks cleaned` : "");
      const a = document.createElement("a");
      a.href = URL.createObjectURL(res.blob);
      a.download = res.name;
      a.className = "btn";
      a.textContent = `Download ${res.name} (${(res.blob.size / 1e6).toFixed(1)} MB)`;
      out.append(a);
    } catch (err) {
      li.classList.replace("running", "err");
      state.textContent = "Couldn't convert";
      out.textContent = err.message || String(err);
    }
  });
}

const take = (files) => [...files].forEach(addJob);
input.addEventListener("change", () => { take(input.files); input.value = ""; });
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
drop.addEventListener("drop", (e) => take(e.dataTransfer.files));

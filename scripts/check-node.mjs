// Fails fast with a readable message when Node is too old for this project.
// (Tailwind 4.3's tooling needs require(esm), which landed in Node 20.19 / 22.12.)
const [major, minor] = process.versions.node.split(".").map(Number);
const ok = major > 22 || (major === 22 && minor >= 12) || (major === 20 && minor >= 19);

if (!ok) {
  console.error(`
  This project needs Node 22.12 or newer. You're on Node ${process.versions.node}.

  Switch with:   nvm use            (reads .nvmrc → Node 24)
  Or for good:   nvm alias default 24
`);
  process.exit(1);
}

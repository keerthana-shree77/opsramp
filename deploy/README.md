# Putting the portal on the VM

Four steps. The first three are about half an hour; the fourth needs one
thing you may have to ask for.

Everything here is typed **on the VM**, not on your laptop.

---

## 1. Get the code onto the VM

The repository is private, so the VM has to prove it is allowed to read it.
The simplest way is a token - a long password that only works for GitHub and
that you can revoke without changing your real one.

**Make the token** (in your browser, on your laptop):

1. Go to `https://github.hpe.com/settings/tokens`
2. **Generate new token (classic)**
3. Note: `firmware portal VM`. Expiry: 90 days.
4. Tick **`repo`** - that one box, nothing else
5. **Generate token**, and copy the long string it shows you

You only get to see it once. It is a password: do not paste it into chat,
into a file, or into a ticket.

**Then, on the VM:**

```bash
git clone https://github.hpe.com/vyshak/opsramp-firmware-tool.git
```

It asks for a username - your GitHub username - and a password. **Paste the
token as the password.** Your account password will not work.

To avoid retyping it on every update:

```bash
cd opsramp-firmware-tool
git config credential.helper 'store --file ~/.git-credentials-hpe'
```

The next pull stores it in that file, readable only by you.

---

## 2. Start it

```bash
cd opsramp-firmware-tool
./deploy/setup.sh
```

The first run creates a `.env` and stops, because it cannot invent your
OpsRamp credentials. It prints exactly which seven values it needs and a
ready-made `SECRET_KEY` you can paste. Fill them in:

```bash
nano .env
```

`Ctrl+O`, `Enter`, `Ctrl+X` to save and quit. Then run it again:

```bash
./deploy/setup.sh
```

It builds, starts, and tells you when it is answering. Copy your OpsRamp
values from the `.env` on your laptop - they are the same ones.

**It now restarts by itself** when the VM reboots. You do not have to do
anything after a restart.

---

## 3. Check it works

```bash
curl -I http://127.0.0.1:8000/login
```

`HTTP/1.1 200 OK` means it is running.

At this point **only the VM itself can reach it.** That is deliberate - the
next step adds the encryption before anyone else is let near it.

---

## 4. Let colleagues reach it, over HTTPS

The portal asks people to sign in, so it must not be reachable over plain
`http://` - a password sent that way is readable by anything between them and
the VM. So the last step is a small piece of software in front that holds the
certificate and does the encryption. This is normal and every internal site
has one.

You need two things, and they are the only things in this whole guide you may
have to ask someone for:

* **A name** for the VM, like `firmware.something.hpe.com`, pointing at its
  address.
* **A certificate** for that name. Whoever issues internal certificates at
  HPE has these; they are routine to request.

With those two files on the VM, install nginx:

```bash
sudo dnf install -y nginx     # or: sudo apt install -y nginx
```

Create `/etc/nginx/conf.d/portal.conf`:

```nginx
server {
    listen 80;
    server_name firmware.example.hpe.com;
    # Anybody arriving unencrypted is sent to the encrypted address.
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name firmware.example.hpe.com;

    ssl_certificate     /etc/ssl/certs/firmware.crt;
    ssl_certificate_key /etc/ssl/private/firmware.key;

    # Recipe uploads; the app's own limit is 10 MiB.
    client_max_body_size 12m;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        # A comparison reads a whole account, which takes a while.
        proxy_read_timeout 300s;
    }
}
```

Replace the three names with your own, then:

```bash
sudo nginx -t && sudo systemctl enable --now nginx
```

`nginx -t` checks the file before anything restarts. If it complains, nothing
has broken yet - fix and try again.

Open the firewall:

```bash
sudo firewall-cmd --permanent --add-service=https && sudo firewall-cmd --reload
```

Your colleagues can now use `https://firmware.example.hpe.com`.

---

## Afterwards

**Publishing a change.** On your laptop I commit and push; then on the VM:

```bash
./deploy/update.sh
```

That is the whole of it. It pulls, rebuilds, restarts, and tells you if the
new version failed to come back - along with the one command that returns you
to the version that worked.

**Everyday commands:**

| | |
|---|---|
| Is it running? | `docker compose ps` |
| What is it doing? | `docker compose logs -f portal` |
| Restart it | `docker compose restart portal` |
| Stop it | `docker compose down` |

**What to back up.** Only the `portal-data` volume, and only if you would
rather not wait for a re-sweep. Nothing on it is irreplaceable - it is a
cache of what OpsRamp already knows.

**What must never be committed.** The `.env`. It holds the OpsRamp client
secret and the portal's own password. It is already ignored by git, and it
should stay that way.

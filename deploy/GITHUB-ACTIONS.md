# GitHub Actions and hosting

The `OpsRamp checks` workflow uses a GitHub-hosted Ubuntu runner to run the
Python tests, build the Docker image, and check the container's login endpoint.
It uses dummy credentials and disables background OpsRamp refreshes for the
container check. No production credentials are needed for this workflow.

It runs on pushes to `main` and pull requests targeting `main`. To start it
manually, open the repository's **Actions** tab, select **OpsRamp checks**,
click **Run workflow**, select `main`, and confirm **Run workflow**.

The runner is temporary. The container built by this workflow is only a
validation build; it is not published or deployed and stops when the job ends.

## Completing deployment

Choose a persistent Linux server or a container hosting service. The existing
`Dockerfile` and `compose.yaml` support a Linux server deployment. See
`deploy/README.md` for the server and HTTPS setup; when cloning this repository,
use `https://github.com/keerthana-shree77/opsramp.git` instead of the older
enterprise GitHub URLs in that guide.

The deployment needs:

- A host that can reach the OpsRamp API and the intended users can reach.
- One running application instance, with one Gunicorn worker and eight threads
  as configured in the Dockerfile.
- Persistent storage mounted at `/data` for the cache and recipe uploads.
- HTTPS and the correct `TRUSTED_PROXY_HOPS` for the hosting arrangement.
- Runtime settings from `.env.example`, including portal login credentials,
  a random `SECRET_KEY`, and the OpsRamp credentials. Keep real values in the
  host's secret configuration or a protected `.env`, never in Git.

The existing hosting request describes an internal service; keep that access
scope when choosing a host unless the intended audience changes.

A deployment workflow must be configured for the selected host and its
authentication method. No deployment destination is configured yet.

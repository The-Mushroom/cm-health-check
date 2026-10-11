# Automatic health checks: setup

How it works: someone submits the Health Check form, WordPress asks GitHub to run the check, GitHub runs it (about 4 minutes) and emails the PDF to you with the subject "A Site Health report for you to read before sending: Business name". You read it and forward it.

## 1. GitHub repository

1. Sign in to GitHub and create a new repository called `cm-health-check`. Set it to **Private**. Leave it empty (no README).
2. Tell Claude the repository name, and Claude can push the files into it for you.

## 2. A mailbox for sending reports

In SiteGround Site Tools, go to **Email > Accounts** and create `reports@creativemushroom.co.uk` with a long random password. This keeps your main mailbox password out of GitHub. Note the outgoing (SMTP) server name shown under **Mail Configuration**, port 465.

## 3. Secrets in GitHub

In the repository: **Settings > Secrets and variables > Actions > New repository secret**. Add each of these:

| Name | Value |
|---|---|
| `PSI_API_KEY` | your Google PageSpeed Insights key |
| `SMTP_HOST` | the outgoing mail server from step 2 |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | `reports@creativemushroom.co.uk` |
| `SMTP_PASSWORD` | that mailbox's password |
| `REPORT_TO` | `hello@creativemushroom.co.uk` |

Secrets are encrypted and nobody, including Claude, can read them back.

## 4. Test it by hand

In the repository, open **Actions > Health check > Run workflow**, enter `creativemushroom.co.uk` and run it. A report email should arrive in about 5 minutes.

## 5. Connect the website

1. In GitHub: your profile picture > **Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token**. Name it "Website form", choose **Only select repositories** > `cm-health-check`, and under Repository permissions set **Contents** to **Read and write**. Set an expiry of a year and put a renewal reminder in your calendar.
2. In SiteGround **File Manager**, open `wp-config.php` and add these two lines above the line that says "That's all, stop editing!":
   ```php
   define( 'CM_GITHUB_REPO', 'your-github-name/cm-health-check' );
   define( 'CM_GITHUB_TOKEN', 'paste-the-token-here' );
   ```
3. Upload `automation/wordpress/cm-health-check-dispatch.php` to `wp-content/mu-plugins/` (create the `mu-plugins` folder if it is not there). Must-use plugins switch on by themselves.
4. Submit a test entry on the Health Check page. The entry in Gravity Forms gets a note saying the check started, and the report arrives by email.

If the hand-off ever fails, the entry is still saved, a note is added to it and the site admin email is told, so no request gets lost.

## Good to know

* GitHub's free allowance covers roughly 500 reports a month.
* Several requests at once simply run side by side.
* Each report is also kept on the run's page in GitHub for 30 days.
* The visitor's name and email pass through GitHub to reach you. Mention GitHub (a US service) as a processor in the privacy policy.
* Google Business Profile and Google position are never automatic. The report email includes the exact command to re-run with them added.

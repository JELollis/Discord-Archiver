# Discord Archive Wiki Setup

This guide implements the design in `Auth_plan.md`:

```text
Discord OAuth2 -> Authentik on Jesse -> OIDC -> MediaWiki
                                           ^
Discord Archiver bot ----------------------+
```

MediaWiki runs on the existing Apache/PHP web servers and uses the existing remote MySQL server. Authentik and its private PostgreSQL database run on Jesse. The bot publishes verified archives to MediaWiki through its API.

## 0. Read this first

- Run one numbered section at a time and verify it before continuing.
- Commands assume Ubuntu and an account with `sudo` access.
- Replace every value in angle brackets, such as `<WIKI_FQDN>`.
- Do not paste real secrets into this repository, shell history, screenshots, or chat.
- Keep the MediaWiki local-login form enabled until Discord login and the break-glass accounts have both been tested.
- Start with one MediaWiki web server. Add the second only after the first is working.
- MediaWiki 1.43 is the current LTS line through December 2027. This guide pins 1.43.9. Review the current security release before installation.

## 1. Deployment worksheet

Fill this out privately before running commands.

| Setting | Example | Your value |
|---|---|---|
| Wiki public URL | `https://wiki.example.com` | `<MEDIAWIKI_PUBLIC_URL>` |
| Wiki hostname | `wiki.example.com` | `<WIKI_FQDN>` |
| Authentik public URL | `https://auth.example.com` | `<AUTHENTIK_PUBLIC_URL>` |
| Authentik hostname | `auth.example.com` | `<AUTH_FQDN>` |
| Jesse private IP | `10.0.0.30` | `<JESSE_IP>` |
| Primary web private IP | `10.0.0.10` | `<WEB1_IP>` |
| Secondary web private IP | `10.0.0.11` | `<WEB2_IP>` |
| MySQL private IP | `10.0.0.20` | `<MYSQL_IP>` |
| Database name | `gtc_wiki` | `<WIKI_DB>` |
| Database user | `gtc_wiki` | `<WIKI_DB_USER>` |
| Discord guild ID | numeric ID | `<DISCORD_GUILD_ID>` |
| Member role ID | numeric ID | `<DISCORD_MEMBER_ROLE_ID>` |
| Editor role ID | numeric ID | `<DISCORD_EDITOR_ROLE_ID>` |
| Archivist role ID | numeric ID | `<DISCORD_ARCHIVIST_ROLE_ID>` |
| Administrator role ID | numeric ID | `<DISCORD_ADMIN_ROLE_ID>` |

Create DNS records before requesting certificates:

```text
wiki.example.com -> current public web-server address
auth.example.com -> Jesse public address
```

If NAT is used, forward TCP 80 and 443 to the appropriate host. Do not expose MySQL, PostgreSQL, Authentik port 9000, or Docker's API to the public internet.

## 2. Preflight every host

Run on Jesse, both web servers, and the MySQL server:

```bash
hostnamectl
cat /etc/os-release
ip -brief address
df -h /
free -h
timedatectl status
```

Install updates and basic tools:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y ca-certificates curl git jq openssl rsync unzip wget
sudo timedatectl set-ntp true
```

Reboot if the upgrade installed a new kernel:

```bash
test -f /var/run/reboot-required && sudo reboot
```

After reconnecting, verify time synchronization:

```bash
timedatectl show -p NTPSynchronized --value
```

Expected: `yes`. OAuth is sensitive to clock drift.

## 3. Install Authentik on Jesse

### 3.1 Install Docker Engine and Compose

Use Docker's official Ubuntu repository:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

Do not add routine users to the `docker` group; membership is effectively root access. Use `sudo docker ...`.

### 3.2 Download the official Authentik Compose definition

```bash
sudo install -d -m 0750 /opt/authentik
cd /opt/authentik
sudo wget -O compose.yml https://docs.goauthentik.io/compose.yml
sudo touch .env
sudo chmod 0600 .env
```

Generate Authentik's PostgreSQL password and application secret without displaying them:

```bash
sudo sh -c 'printf "PG_PASS=%s\n" "$(openssl rand -base64 36 | tr -d "\n")" >> /opt/authentik/.env'
sudo sh -c 'printf "AUTHENTIK_SECRET_KEY=%s\n" "$(openssl rand -base64 60 | tr -d "\n")" >> /opt/authentik/.env'
```

Keep Authentik on its default host ports. Apache will proxy HTTPS to local port 9000.

```bash
cd /opt/authentik
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=100 server worker
```

Verify locally on Jesse:

```bash
curl -I http://127.0.0.1:9000/
```

### 3.3 Put Apache and TLS in front of Authentik

If Jesse already has Apache, the install command will preserve the existing configuration.

```bash
sudo apt install -y apache2 certbot python3-certbot-apache
sudo a2enmod proxy proxy_http headers rewrite ssl
```

Create `/etc/apache2/sites-available/authentik.conf`:

```apache
<VirtualHost *:80>
    ServerName <AUTH_FQDN>
    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "http"
    ProxyPass / http://127.0.0.1:9000/
    ProxyPassReverse / http://127.0.0.1:9000/
    ErrorLog ${APACHE_LOG_DIR}/authentik-error.log
    CustomLog ${APACHE_LOG_DIR}/authentik-access.log combined
</VirtualHost>
```

Enable and test it:

```bash
sudo a2ensite authentik.conf
sudo apache2ctl configtest
sudo systemctl reload apache2
sudo certbot --apache -d <AUTH_FQDN>
```

After Certbot succeeds, confirm that the HTTPS virtual host retains these directives:

```apache
ProxyPreserveHost On
RequestHeader set X-Forwarded-Proto "https"
ProxyPass / http://127.0.0.1:9000/
ProxyPassReverse / http://127.0.0.1:9000/
```

Block direct public access to Authentik's container ports. If UFW is already active:

```bash
sudo ufw deny 9000/tcp
sudo ufw deny 9443/tcp
sudo ufw allow 'Apache Full'
sudo ufw status numbered
```

Do not enable or reset UFW remotely without first confirming that SSH is allowed.

### 3.4 Complete Authentik's initial setup

Open:

```text
https://<AUTH_FQDN>/if/flow/initial-setup/
```

Create the `akadmin` password and store it in a password manager. If the setup flow fails, inspect:

```bash
cd /opt/authentik
sudo docker compose ps
sudo docker compose logs --tail=200 server worker postgresql
```

## 4. Configure Discord as Authentik's login source

### 4.1 Discord Developer Portal

1. Open the Discord Developer Portal.
2. Create an application named `GTC Archive Wiki`.
3. Open **OAuth2** and create/reset the client secret.
4. Record the client ID and secret in the password manager.
5. Add this exact redirect URI:

```text
https://<AUTH_FQDN>/source/oauth/callback/discord/
```

6. Enable Discord Developer Mode and copy the guild and role IDs into the private worksheet.

### 4.2 Create Authentik groups

In Authentik Admin, go to **Directory > Groups** and create:

- `gtc-member`
- `gtc-editor`
- `gtc-archivist`
- `gtc-admin`

Edit each group's attributes and assign its Discord role ID:

```yaml
discord_role_id: "<CORRESPONDING_DISCORD_ROLE_ID>"
```

### 4.3 Create the Discord source

Go to **Directory > Federation and Social login > New Source**:

- Type: **Discord OAuth Source**
- Name: `Discord`
- Slug: `discord`
- Consumer key: Discord client ID
- Consumer secret: Discord client secret
- Scopes: `identify guilds guilds.members.read`

Do not request `email` unless it becomes necessary.

### 4.4 Restrict access and synchronize roles

Use Authentik's current official Discord guide linked under References. It supplies maintained expressions for:

- Checking membership in one Discord guild
- Reading Discord role IDs
- Synchronizing matching roles into Authentik groups

For the role-sync mapping:

1. Set `ACCEPTED_GUILD_ID` to `<DISCORD_GUILD_ID>`.
2. Select the mapping on the Discord source under **OAuth Attribute mapping**.
3. Bind the guild-membership policy to the Discord source's enrollment and authentication flows.
4. Test with one guild member and one Discord user who is not in the guild.

Do not invent or copy old expressions from archived walkthroughs; Authentik's user/group API changed in 2026.

## 5. Create the MediaWiki MySQL database

Run on the MySQL server. Replace both web-server IPs and generate a unique password in a password manager.

```bash
sudo mysql
```

Then run:

```sql
CREATE DATABASE `<WIKI_DB>` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER '<WIKI_DB_USER>'@'<WEB1_IP>' IDENTIFIED BY '<LONG_RANDOM_DB_PASSWORD>';
GRANT ALL PRIVILEGES ON `<WIKI_DB>`.* TO '<WIKI_DB_USER>'@'<WEB1_IP>';
CREATE USER '<WIKI_DB_USER>'@'<WEB2_IP>' IDENTIFIED BY '<LONG_RANDOM_DB_PASSWORD>';
GRANT ALL PRIVILEGES ON `<WIKI_DB>`.* TO '<WIKI_DB_USER>'@'<WEB2_IP>';
FLUSH PRIVILEGES;
EXIT;
```

Restrict TCP 3306 at the host firewall and network edge to `<WEB1_IP>` and `<WEB2_IP>` only. Verify that MySQL is listening on the intended private interface rather than a public interface.

Test from each web server without putting the password on the command line:

```bash
mysql -h <MYSQL_IP> -u <WIKI_DB_USER> -p -e 'SELECT VERSION();'
```

## 6. Install MediaWiki on the primary web server

### 6.1 Verify PHP compatibility

MediaWiki 1.43 LTS supports PHP 8.1 through 8.4. Check the server before continuing:

```bash
php -v
apache2ctl -v
```

Install the required packages:

```bash
sudo apt update
sudo apt install -y apache2 libapache2-mod-php php php-apcu php-cli php-curl php-gd php-intl php-mbstring php-mysql php-xml php-zip mariadb-client composer certbot python3-certbot-apache
sudo a2enmod rewrite headers ssl
```

### 6.2 Download MediaWiki 1.43 LTS

```bash
cd /tmp
wget https://releases.wikimedia.org/mediawiki/1.43/mediawiki-1.43.9.tar.gz
wget https://releases.wikimedia.org/mediawiki/1.43/mediawiki-1.43.9.tar.gz.sig
tar -tzf mediawiki-1.43.9.tar.gz >/dev/null
sudo tar -xzf mediawiki-1.43.9.tar.gz -C /var/www/
sudo ln -sfn /var/www/mediawiki-1.43.9 /var/www/mediawiki
sudo chown -R root:www-data /var/www/mediawiki-1.43.9
sudo find /var/www/mediawiki-1.43.9 -type d -exec chmod 0755 {} \;
sudo find /var/www/mediawiki-1.43.9 -type f -exec chmod 0644 {} \;
sudo chmod -R 0770 /var/www/mediawiki-1.43.9/images
```

The archive integrity check above detects a damaged download. For cryptographic verification, import and validate the current MediaWiki release signing key from the official download page before production use.

### 6.3 Configure Apache

Create `/etc/apache2/sites-available/gtc-wiki.conf`:

```apache
<VirtualHost *:80>
    ServerName <WIKI_FQDN>
    DocumentRoot /var/www/mediawiki

    <Directory /var/www/mediawiki>
        Options FollowSymLinks
        AllowOverride None
        Require all granted
        DirectoryIndex index.php
    </Directory>

    <FilesMatch "^(LocalSettings\.php|composer\.(json|lock)|\.env)$">
        Require all denied
    </FilesMatch>

    ErrorLog ${APACHE_LOG_DIR}/gtc-wiki-error.log
    CustomLog ${APACHE_LOG_DIR}/gtc-wiki-access.log combined
</VirtualHost>
```

Enable it and issue the certificate:

```bash
sudo a2ensite gtc-wiki.conf
sudo apache2ctl configtest
sudo systemctl reload apache2
sudo certbot --apache -d <WIKI_FQDN>
```

### 6.4 Run MediaWiki's command-line installer

Generate and store two private values first:

- A temporary installation administrator password
- The MySQL password created in section 5

Run the installer from the MediaWiki root. It will prompt for the database password if `--dbpass` is omitted; omitting it keeps the database password out of shell history. Read the temporary administrator password into a non-echoed shell variable for the same reason.

```bash
cd /var/www/mediawiki
read -rsp 'Temporary MediaWiki administrator password: ' MW_ADMIN_PASS
echo
sudo -u www-data php maintenance/run.php install \
  --dbname '<WIKI_DB>' \
  --dbserver '<MYSQL_IP>' \
  --dbuser '<WIKI_DB_USER>' \
  --server 'https://<WIKI_FQDN>' \
  --scriptpath '' \
  --lang en \
  --pass "$MW_ADMIN_PASS" \
  'GTC Discord Archive' '<BREAK_GLASS_ADMIN_NAME>'
unset MW_ADMIN_PASS
```

Move database credentials out of the web root. Create `/etc/mediawiki/private.php`:

```bash
sudo install -d -m 0750 -o root -g www-data /etc/mediawiki
sudo nano /etc/mediawiki/private.php
```

```php
<?php
$wgDBserver = '<MYSQL_IP>';
$wgDBname = '<WIKI_DB>';
$wgDBuser = '<WIKI_DB_USER>';
$wgDBpassword = '<LONG_RANDOM_DB_PASSWORD>';
$wgOIDCClientID = '<AUTHENTIK_OIDC_CLIENT_ID>';
$wgOIDCClientSecret = '<AUTHENTIK_OIDC_CLIENT_SECRET>';
```

Protect it:

```bash
sudo chown root:www-data /etc/mediawiki/private.php
sudo chmod 0640 /etc/mediawiki/private.php
```

In `LocalSettings.php`, replace the generated database-secret assignments with:

```php
require '/etc/mediawiki/private.php';
```

Do not delete `$wgSecretKey` or `$wgAuthenticationTokenVersion`; both web servers must eventually use identical values.

## 7. Install MediaWiki authentication extensions

Use extension branches matching MediaWiki 1.43:

```bash
cd /var/www/mediawiki/extensions
sudo git clone --branch REL1_43 https://gerrit.wikimedia.org/r/mediawiki/extensions/PluggableAuth
sudo git clone --branch REL1_43 https://gerrit.wikimedia.org/r/mediawiki/extensions/OpenIDConnect
sudo chown -R root:www-data PluggableAuth OpenIDConnect
```

Create `/var/www/mediawiki/composer.local.json`:

```json
{
  "extra": {
    "merge-plugin": {
      "include": [
        "extensions/OpenIDConnect/composer.json"
      ]
    }
  }
}
```

Install dependencies and update the database:

```bash
cd /var/www/mediawiki
sudo -u www-data composer update --no-dev --optimize-autoloader
sudo -u www-data php maintenance/run.php update --quick
```

## 8. Create the Authentik OIDC application

In Authentik Admin:

1. Go to **Applications > Applications > Create with provider**.
2. Application name: `GTC Archive Wiki`.
3. Application slug: `gtc-archive-wiki`.
4. Provider type: **OAuth2/OpenID Provider**.
5. Client type: **Confidential**.
6. Flow: authorization code.
7. Redirect URI, exact match:

```text
https://<WIKI_FQDN>/Special:PluggableAuthLogin
```

8. Use per-provider issuer mode.
9. Record the generated client ID and secret in `/etc/mediawiki/private.php` on the web server.
10. Confirm the discovery document loads:

```bash
curl -fsS https://<AUTH_FQDN>/application/o/gtc-archive-wiki/.well-known/openid-configuration | jq .issuer
```

Create an OAuth2/OpenID scope mapping named `GTC groups` that returns a `groups` claim containing the user's direct and inherited Authentik group names. Use the current Authentik user API (`request.user.all_groups()`), then add that scope mapping to the provider. The expected claim values are `gtc-member`, `gtc-editor`, `gtc-archivist`, and `gtc-admin`.

## 9. Configure MediaWiki OIDC and permissions

Append the following to `LocalSettings.php`. Keep local login enabled during testing.

```php
wfLoadExtension( 'PluggableAuth' );
wfLoadExtension( 'OpenIDConnect' );

$wgServer = 'https://<WIKI_FQDN>';
$wgCanonicalServer = $wgServer;

$wgPluggableAuth_EnableAutoLogin = false;
$wgPluggableAuth_EnableLocalLogin = true;
$wgPluggableAuth_EnableLocalProperties = false;

$wgPluggableAuth_Config = [
    'Discord' => [
        'plugin' => 'OpenIDConnect',
        'data' => [
            'providerURL' => 'https://<AUTH_FQDN>/application/o/gtc-archive-wiki/',
            'clientID' => $wgOIDCClientID,
            'clientsecret' => $wgOIDCClientSecret,
            'scope' => [ 'openid', 'profile', 'groups' ],
            'preferred_username' => 'preferred_username',
            'verifyHost' => true,
            'verifyPeer' => true,
        ],
        'groupsyncs' => [
            [
                'type' => 'mapped',
                'map' => [
                    'member' => [ 'groups' => 'gtc-member' ],
                    'editor' => [ 'groups' => 'gtc-editor' ],
                    'archivist' => [ 'groups' => 'gtc-archivist' ],
                    'sysop' => [ 'groups' => 'gtc-admin' ],
                ],
                'addOnlyGroups' => [ 'sysop' ],
            ],
        ],
    ],
];

// Private wiki: no anonymous read, edit, or account creation.
$wgGroupPermissions['*']['read'] = false;
$wgGroupPermissions['*']['edit'] = false;
$wgGroupPermissions['*']['createaccount'] = false;
$wgGroupPermissions['*']['autocreateaccount'] = true;

// Define the project groups.
$wgGroupPermissions['member']['read'] = true;
$wgGroupPermissions['editor']['read'] = true;
$wgGroupPermissions['editor']['edit'] = true;
$wgGroupPermissions['archivist']['read'] = true;
$wgGroupPermissions['archivist']['edit'] = true;
$wgGroupPermissions['archivist']['upload'] = true;
$wgGroupPermissions['archivist']['reupload'] = true;
```

Validate PHP syntax and reload Apache:

```bash
php -l /var/www/mediawiki/LocalSettings.php
sudo apache2ctl configtest
sudo systemctl reload apache2
```

Verify extensions at:

```text
https://<WIKI_FQDN>/Special:Version
```

## 10. Test before locking login down

Use a private/incognito browser for each test.

1. Local break-glass administrator can log in.
2. Discord user outside the guild is denied.
3. Guild member receives MediaWiki `member`.
4. Editor receives `editor`.
5. Archivist receives `archivist`.
6. Discord administrator receives `sysop`.
7. Removing a Discord role removes the corresponding MediaWiki group on the next login, except `sysop`, which is add-only for break-glass safety.
8. Logout works.
9. The wiki is unreadable in a signed-out browser.
10. `Special:Version` lists PluggableAuth and OpenID Connect.

Inspect failures:

```bash
sudo tail -n 200 /var/log/apache2/gtc-wiki-error.log
cd /opt/authentik && sudo docker compose logs --tail=200 server worker
```

Only after every test passes, change MediaWiki to Discord-first login:

```php
$wgPluggableAuth_EnableAutoLogin = true;
$wgPluggableAuth_EnableLocalLogin = false;
```

Revalidate and reload:

```bash
php -l /var/www/mediawiki/LocalSettings.php
sudo systemctl reload apache2
```

## 11. Create break-glass accounts

Keep two or three local `sysop` accounts. Use unique random passwords and no routine use. Create each account while local login is temporarily enabled, then add it to `sysop` from `Special:UserRights`.

Store for each account:

- Username
- Password-manager entry
- Creation date
- Last successful emergency test
- Person responsible

Do not give the bot a break-glass or `sysop` account.

### Emergency local-login procedure

If Discord or Authentik is unavailable, edit `LocalSettings.php`:

```php
$wgPluggableAuth_EnableAutoLogin = false;
$wgPluggableAuth_EnableLocalLogin = true;
```

Then:

```bash
php -l /var/www/mediawiki/LocalSettings.php
sudo systemctl reload apache2
```

After recovery, test Discord SSO, restore the two production values, reload Apache, and document who used emergency access and why.

## 12. Add the secondary MediaWiki server

Do not run the web installer again. Install the same OS packages and extract the exact same MediaWiki release on the secondary server. Copy these from the primary over the private network:

- `LocalSettings.php`
- `/etc/mediawiki/private.php`
- `extensions/PluggableAuth`
- `extensions/OpenIDConnect`
- `composer.local.json`
- `composer.lock`
- `vendor/`

Example from the primary server:

```bash
sudo rsync -aHAX --numeric-ids /var/www/mediawiki-1.43.9/ <ADMIN_USER>@<WEB2_IP>:/tmp/mediawiki-1.43.9/
sudo scp /etc/mediawiki/private.php <ADMIN_USER>@<WEB2_IP>:/tmp/private.php
```

On the secondary server, move the files into place with `sudo`, preserve the same permissions, configure the same Apache virtual host, and test against a temporary hosts-file entry before sending production traffic to it.

Both servers must share the same:

- MySQL database
- `$wgSecretKey` and authentication-token version
- OIDC client credentials and public URL
- extension/core versions
- uploads
- session state, or load-balancer session affinity

For the initial rollout, keep the secondary out of rotation until shared uploads and sessions are implemented and tested. Do not independently accept uploads on two unsynchronized `images/` directories.

## 13. Create the bot service account

Keep this separate from human OIDC accounts.

1. Temporarily enable local login.
2. Create a local service account such as `DiscordArchiveBot` with a long random password.
3. Create a MediaWiki bot password at `Special:BotPasswords` with only the grants required to edit archive pages and upload files.
4. Store only the bot-password username and secret in the archiver host's protected environment file.
5. Disable local login again.

Suggested environment variable names:

```dotenv
MEDIAWIKI_API_URL=https://<WIKI_FQDN>/api.php
MEDIAWIKI_BOT_USERNAME=DiscordArchiveBot@ArchivePublisher
MEDIAWIKI_BOT_PASSWORD=<BOT_PASSWORD>
MEDIAWIKI_ARCHIVE_NAMESPACE=Archive
```

Protect the bot environment file:

```bash
sudo chown root:<BOT_SERVICE_GROUP> /etc/discord-archiver/wiki.env
sudo chmod 0640 /etc/discord-archiver/wiki.env
```

Before permitting channel deletion, the bot must upload attachments, publish the page, read the saved revision ID back from MediaWiki, verify content, and post the permanent page URL to `#archives`.

## 14. Backups

### Authentik on Jesse

Create an encrypted off-host backup destination before relying on Authentik. A logical PostgreSQL backup can be produced with:

```bash
cd /opt/authentik
sudo docker compose exec -T postgresql pg_dump -U authentik authentik | gzip > /tmp/authentik-$(date -u +%F).sql.gz
sudo cp .env /tmp/authentik-env-$(date -u +%F)
sudo chmod 0600 /tmp/authentik-env-$(date -u +%F)
```

Immediately transfer both files to encrypted off-host storage and remove the temporary copies. The `.env` backup contains secrets.

### MediaWiki

Back up together:

- MySQL database
- `LocalSettings.php`
- `/etc/mediawiki/private.php`
- `images/`
- installed extension versions

Example database dump on the MySQL host:

```bash
mysqldump --single-transaction --routines --triggers -u root -p <WIKI_DB> | gzip > /tmp/<WIKI_DB>-$(date -u +%F).sql.gz
```

Test restoration on a non-production host at least once per semester.

## 15. Updates

Never update production first. Back up and test on staging.

Authentik Compose update pattern:

```bash
cd /opt/authentik
sudo cp compose.yml compose.yml.before-update
sudo wget -O compose.yml https://docs.goauthentik.io/compose.yml
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=100 server worker
```

MediaWiki updates must keep core, PluggableAuth, and OpenIDConnect on compatible release branches. Review MediaWiki release notes, back up the database and files, replace core, update extensions, run:

```bash
sudo -u www-data php maintenance/run.php update --quick
php -l /var/www/mediawiki/LocalSettings.php
sudo apache2ctl configtest
sudo systemctl reload apache2
```

## 16. Final acceptance checklist

- [ ] DNS and valid HTTPS certificates work for wiki and Authentik.
- [ ] Authentik ports 9000/9443 are not publicly reachable.
- [ ] PostgreSQL is private to the Authentik Compose stack.
- [ ] MySQL accepts the wiki user only from the two web-server IPs.
- [ ] Discord non-members are denied.
- [ ] Discord roles map to the intended MediaWiki groups.
- [ ] Anonymous users cannot read or edit.
- [ ] Local login is hidden during normal operation.
- [ ] Two or three break-glass accounts have been tested.
- [ ] The bot uses a limited bot password, not a human password.
- [ ] Bot read-back verification succeeds before any Discord deletion.
- [ ] Authentik and MediaWiki backups exist off-host.
- [ ] The secondary web server remains out of rotation until uploads and sessions are shared.

## References

- [Authentik Docker Compose installation](https://docs.goauthentik.io/install-config/install/docker-compose/)
- [Authentik Discord source](https://docs.goauthentik.io/users-sources/sources/social-logins/discord/)
- [Authentik OAuth2/OIDC provider](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/)
- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [MediaWiki 1.43 LTS lifecycle](https://www.mediawiki.org/wiki/Version_lifecycle)
- [Installing MediaWiki](https://www.mediawiki.org/wiki/Manual:Installing_MediaWiki)
- [MediaWiki on Debian/Ubuntu](https://www.mediawiki.org/wiki/Manual:Running_MediaWiki_on_Debian_or_Ubuntu)
- [PluggableAuth](https://www.mediawiki.org/wiki/Extension:PluggableAuth)
- [OpenID Connect extension](https://www.mediawiki.org/wiki/Extension:OpenID_Connect)

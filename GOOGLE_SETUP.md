# Google Service Account Setup

The app needs a real Google service account JSON file. This file must be downloaded from Google Cloud; it cannot be created locally without Google issuing the key.

## Create The File

1. Go to Google Cloud Console:
   `https://console.cloud.google.com/`
2. Create or select a project.
3. Open `APIs & Services` > `Library`.
4. Enable these APIs:
   - Google Sheets API
   - Google Drive API
5. Open `IAM & Admin` > `Service Accounts`.
6. Click `Create service account`.
7. Name it something like `expenditure-ai`.
8. Finish the creation flow.
9. Open the new service account.
10. Go to `Keys` > `Add key` > `Create new key`.
11. Choose `JSON`.
12. Download the JSON file.

## Put It In This Project

Rename the downloaded file to:

```text
service_account.json
```

Put it here:

```text
C:\Users\user\Documents\ExpenditureAI\service_account.json
```

Your `.env` already expects this:

```text
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
```

## Share Your Drive Folder

1. Open `service_account.json`.
2. Copy the `client_email` value.
3. Open your Google Drive `Expenditure` folder.
4. Click `Share`.
5. Paste the service account email.
6. Give it `Editor` access.

After this, the app can create/update the year files and month tabs in that folder.

## Important

Do not share or upload `service_account.json`. It is already ignored by `.gitignore`.

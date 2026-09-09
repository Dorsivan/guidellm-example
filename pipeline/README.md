# Creating an API Token for an OpenShift User

This guide covers how to create an OpenShift user via `oc create user` and assign it an API token for programmatic access.

## Prerequisites

- Cluster admin access
- `oc` CLI installed and logged in
- `openssl` available (for token generation)

## Step 1: Create the User

```bash
oc create user myuser
```

## Step 2: Assign Roles

```bash
oc adm policy add-role-to-user edit myuser -n my-namespace
```

## Step 3: Create an API Token

### Option 1: Create an OAuthAccessToken Directly

Generate a token and create the corresponding `OAuthAccessToken` resource:

```bash
TOKEN=$(openssl rand -hex 32)

oc create -f - <<EOF
apiVersion: oauth.openshift.io/v1
kind: OAuthAccessToken
metadata:
  name: sha256~$(echo -n "$TOKEN" | sha256sum | cut -d' ' -f1)
clientName: openshift-challenging-client
userName: myuser
userUID: $(oc get user myuser -o jsonpath='{.metadata.uid}')
scopes:
  - "user:full"
expiresIn: 86400
redirectURI: https://localhost
EOF
```

Use the token:

```bash
oc login --token="sha256~${TOKEN}" --server=https://<api-server>:6443
```

Or pass it directly in API calls:

```bash
curl -k -H "Authorization: Bearer sha256~${TOKEN}" \
  https://<api-server>:6443/api/v1/namespaces/my-namespace/pods
```

### Option 2: Create a Token via the API

Use the API directly from an admin context:

```bash
ADMIN_TOKEN=$(oc whoami -t)
USER_UID=$(oc get user myuser -o jsonpath='{.metadata.uid}')

curl -k -X POST \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  "https://<api-server>:6443/apis/oauth.openshift.io/v1/oauthaccesstokens" \
  -d '{
    "kind": "OAuthAccessToken",
    "apiVersion": "oauth.openshift.io/v1",
    "metadata": { "generateName": "sha256~" },
    "clientName": "openshift-challenging-client",
    "userName": "myuser",
    "userUID": "'"$USER_UID"'",
    "scopes": ["user:full"],
    "expiresIn": 86400
  }'
```

### Option 3: Impersonation

If your automation runs as a cluster admin, you can impersonate the user without managing tokens:

```bash
oc get pods --as=myuser -n my-namespace
```

## Notes

- `expiresIn` is in seconds. `86400` = 24 hours. Adjust as needed.
- Tokens created as `OAuthAccessToken` objects can be listed and deleted with:
  ```bash
  oc get oauthaccesstokens | grep myuser
  oc delete oauthaccesstoken <token-name>
  ```
- To automate token rotation, re-run the token creation step on a schedule and update any consumers with the new token value.

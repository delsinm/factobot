# Deploying Factobot to AWS

This guide covers deploying the bot to AWS using **ECS Fargate** — AWS's
managed container platform. Fargate runs the bot as a persistent long-running
container, which is the right fit for Socket Mode (the bot holds an open
WebSocket connection to Slack rather than serving HTTP requests).

Secrets are stored in **AWS Secrets Manager** and injected into the container
at runtime — no credentials are ever baked into the image or stored in source
control.

Icons are hosted on **S3** with a public read policy so Slack can fetch them
server-side when rendering modals.

---

## Architecture Overview

```
GitHub repo
    │
    ▼
ECR (Elastic Container Registry)
    │  Docker image pushed here
    ▼
ECS Fargate (task running main.py)
    │  reads secrets at startup
    ▼
AWS Secrets Manager
    │  SLACK_BOT_TOKEN, ANTHROPIC_API_KEY, etc.

ECS Fargate
    │  outbound WebSocket connection
    ▼
Slack (Socket Mode)

Workflow receivers (Make, Zapier, n8n, etc.)
    │  HTTPS callback to your domain
    ▼
Application Load Balancer  ←── ACM certificate (TLS termination)
    │  HTTP on port 3000 (internal VPC only)
    ▼
ECS Fargate (Flask callback server)
    │  DMs result to submitter
    ▼
Slack

S3 bucket (public read)
    │  info/ack/error icon URLs
    ▼
Slack (fetches icons server-side when rendering modals)
```

**Why Fargate over Lambda?** The bot uses Socket Mode, which holds a persistent
WebSocket connection open. Lambda functions time out after a maximum of 15
minutes and are not suited for persistent connections. Fargate runs your
container indefinitely like a regular server, but without you managing EC2
instances.

**Why not EC2?** Fargate handles patching, scaling, and instance management.
For a single bot instance, EC2 adds operational overhead with no benefit.

---

## Prerequisites

- An AWS account with permissions to create ECS, ECR, IAM, Secrets Manager,
  VPC, and S3 resources
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed and configured (`aws configure`)
- [Docker](https://docs.docker.com/get-docker/) installed locally
- The bot's Slack app already created (see main README → Setup → Step 1)
- Your API keys and tokens ready to paste

---

## Step 1 — Create a Dockerfile

Add this file to the project root alongside `main.py`:

```dockerfile
# Dockerfile
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies first (cached layer — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# The bot connects outbound to Slack via WebSocket — no port needs to be exposed
CMD ["python", "main.py"]
```

Build and test locally before pushing to AWS:

```bash
docker build -t factobot .
docker run --env-file .env factobot
```

If the bot connects and you see `⚡ Bolt app is running!` in the logs, the image is good.

---

## Step 2 — Store Secrets in AWS Secrets Manager

Store each secret individually so you can rotate them independently and grant
fine-grained IAM access per secret.

```bash
# Required secrets
aws secretsmanager create-secret \
  --name factobot/SLACK_BOT_TOKEN \
  --secret-string "xoxb-your-bot-token"

aws secretsmanager create-secret \
  --name factobot/SLACK_APP_TOKEN \
  --secret-string "xapp-your-app-level-token"

aws secretsmanager create-secret \
  --name factobot/ANTHROPIC_API_KEY \
  --secret-string "sk-ant-your-api-key"

# Optional secrets — only create if you use them
aws secretsmanager create-secret \
  --name factobot/MAKE_WEBHOOK_URL \
  --secret-string "https://hook.us1.make.com/your-webhook-id"
```

Non-sensitive configuration (BOT_NAME, icon URLs, AI_MODEL) goes directly
in the ECS task definition as plain environment variables rather than secrets —
they're not credentials and don't need encryption.

**To update a secret later** (e.g. rotating a token):

```bash
aws secretsmanager put-secret-value \
  --secret-id factobot/SLACK_BOT_TOKEN \
  --secret-string "xoxb-your-new-token"
```

ECS picks up the new value the next time the task starts. Force a restart:

```bash
aws ecs update-service \
  --cluster factobot-cluster \
  --service factobot-service \
  --force-new-deployment
```

---

## Step 3 — Create an ECR Repository and Push the Image

ECR is AWS's private Docker registry. The ECS task pulls the image from here.

```bash
# Set your AWS account ID and region
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1    # change to your preferred region

# Create the ECR repository
aws ecr create-repository --repository-name factobot --region $REGION

# Authenticate Docker with ECR
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin \
    $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

# Build, tag, and push the image
docker build -t factobot .

docker tag factobot:latest \
  $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/factobot:latest

docker push \
  $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/factobot:latest
```

Note the full image URI — you'll need it in Step 5:
```
123456789012.dkr.ecr.us-east-1.amazonaws.com/factobot:latest
```

---

## Step 4 — Create IAM Roles

ECS needs two IAM roles:

**Task Execution Role** — used by ECS itself to pull the image from ECR and
fetch secrets from Secrets Manager before starting the container.

**Task Role** — used by the running container if it needs to make AWS API
calls at runtime (not required for this bot, but good practice to create).

### Task Execution Role

```bash
# Create the role
aws iam create-role \
  --role-name factobot-task-execution-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ecs-tasks.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# Attach the AWS-managed ECS execution policy (allows ECR pulls and CloudWatch logs)
aws iam attach-role-policy \
  --role-name factobot-task-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# Grant access to read the factobot/* secrets from Secrets Manager
aws iam put-role-policy \
  --role-name factobot-task-execution-role \
  --policy-name factobot-secrets-access \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:us-east-1:*:secret:factobot/*"
    }]
  }'
```

> Replace `us-east-1` with your region in the Resource ARN above.

### Task Role

```bash
aws iam create-role \
  --role-name factobot-task-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ecs-tasks.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
```

---

## Step 5 — Create the ECS Task Definition

The task definition tells ECS how to run the container — which image to use,
how much CPU/memory to allocate, which secrets to inject, and where to send logs.

Save the following as `task-definition.json`, substituting your values:

```json
{
  "family": "factobot",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256",
  "memory": "512",
  "executionRoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/factobot-task-execution-role",
  "taskRoleArn":      "arn:aws:iam::YOUR_ACCOUNT_ID:role/factobot-task-role",
  "containerDefinitions": [
    {
      "name": "factobot",
      "image": "YOUR_ACCOUNT_ID.dkr.ecr.YOUR_REGION.amazonaws.com/factobot:latest",
      "essential": true,

      "secrets": [
        {
          "name": "SLACK_BOT_TOKEN",
          "valueFrom": "arn:aws:secretsmanager:YOUR_REGION:YOUR_ACCOUNT_ID:secret:factobot/SLACK_BOT_TOKEN"
        },
        {
          "name": "SLACK_APP_TOKEN",
          "valueFrom": "arn:aws:secretsmanager:YOUR_REGION:YOUR_ACCOUNT_ID:secret:factobot/SLACK_APP_TOKEN"
        },
        {
          "name": "ANTHROPIC_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:YOUR_REGION:YOUR_ACCOUNT_ID:secret:factobot/ANTHROPIC_API_KEY"
        },
        {
          "name": "MAKE_WEBHOOK_URL",
          "valueFrom": "arn:aws:secretsmanager:YOUR_REGION:YOUR_ACCOUNT_ID:secret:factobot/MAKE_WEBHOOK_URL"
        }
      ],

      "environment": [
        {"name": "BOT_NAME",       "value": "factobot"},
        {"name": "AI_MODEL",       "value": "anthropic/claude-sonnet-4-20250514"},
        {"name": "MAX_HISTORY",    "value": "20"},
        {"name": "INFO_ICON_URL",  "value": "https://your-bucket.s3.us-east-1.amazonaws.com/info-icon.png"},
        {"name": "ACK_ICON_URL",   "value": "https://your-bucket.s3.us-east-1.amazonaws.com/ack-icon.png"},
        {"name": "ERROR_ICON_URL", "value": "https://your-bucket.s3.us-east-1.amazonaws.com/error-icon.png"}
      ],

      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group":         "/ecs/factobot",
          "awslogs-region":        "YOUR_REGION",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

**CPU and memory** — `256` CPU units (0.25 vCPU) and `512` MB RAM is plenty
for a single-instance bot. Increase if you add heavy processing.

Register the task definition:

```bash
aws ecs register-task-definition --cli-input-json file://task-definition.json
```

---

## Step 6 — Create a CloudWatch Log Group

ECS sends container logs here. Create it before starting the service or the
container will fail to start due to the missing log group.

```bash
aws logs create-log-group --log-group-name /ecs/factobot

# Optional: set a retention policy (keeps 30 days of logs, then auto-deletes)
aws logs put-retention-policy \
  --log-group-name /ecs/factobot \
  --retention-in-days 30
```

---

## Step 7 — Create an ECS Cluster and Service

### Create the cluster

```bash
aws ecs create-cluster --cluster-name factobot-cluster
```

### Find your default VPC and a subnet

```bash
# Get the default VPC ID
VPC_ID=$(aws ec2 describe-vpcs \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' \
  --output text)

echo "VPC: $VPC_ID"

# Get a subnet in that VPC
SUBNET_ID=$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values=$VPC_ID \
  --query 'Subnets[0].SubnetId' \
  --output text)

echo "Subnet: $SUBNET_ID"
```

### Create security groups

Two security groups are needed: one for the ALB (accepts public HTTPS) and one
for the ECS task (accepts port 3000 only from the ALB).

```bash
# --- ALB security group ---
ALB_SG_ID=$(aws ec2 create-security-group \
  --group-name factobot-alb-sg \
  --description "Factobot ALB — public HTTPS ingress" \
  --vpc-id $VPC_ID \
  --query 'GroupId' \
  --output text)

echo "ALB security group: $ALB_SG_ID"

# Allow inbound HTTPS from anywhere
aws ec2 authorize-security-group-ingress \
  --group-id $ALB_SG_ID \
  --protocol tcp --port 443 \
  --cidr 0.0.0.0/0

# Allow inbound HTTP from anywhere (ALB redirects to HTTPS; see Step 7b)
aws ec2 authorize-security-group-ingress \
  --group-id $ALB_SG_ID \
  --protocol tcp --port 80 \
  --cidr 0.0.0.0/0

# Allow all outbound (ALB needs to reach ECS tasks)
aws ec2 authorize-security-group-egress \
  --group-id $ALB_SG_ID \
  --protocol -1 \
  --cidr 0.0.0.0/0

# --- ECS task security group ---
SG_ID=$(aws ec2 create-security-group \
  --group-name factobot-sg \
  --description "Factobot ECS task — ALB ingress only" \
  --vpc-id $VPC_ID \
  --query 'GroupId' \
  --output text)

echo "ECS task security group: $SG_ID"

# Allow port 3000 (Flask callback server) ONLY from the ALB security group.
# The task is not directly reachable from the public internet.
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 3000 \
  --source-group $ALB_SG_ID

# Allow all outbound (Slack WebSocket, AI API, webhook receivers)
aws ec2 authorize-security-group-egress \
  --group-id $SG_ID \
  --protocol -1 \
  --cidr 0.0.0.0/0
```

### Create the ECS service

```bash
aws ecs create-service \
  --cluster factobot-cluster \
  --service-name factobot-service \
  --task-definition factobot \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={
    subnets=[$SUBNET_ID],
    securityGroups=[$SG_ID],
    assignPublicIp=ENABLED
  }" \
  --load-balancers "targetGroupArn=$TARGET_GROUP_ARN,containerName=factobot,containerPort=3000"
```

> **Note:** `$TARGET_GROUP_ARN` is set in Step 7b below. If you are not
> enabling callbacks (running fire-and-forget mode only), omit the
> `--load-balancers` argument and the ALB setup entirely — the task needs
> no inbound port in that case.

`desired-count 1` runs exactly one container instance. The bot maintains a
single WebSocket connection to Slack — running multiple instances would cause
duplicate responses to every Slack event.

`assignPublicIp=ENABLED` is still needed here so the task can reach Slack's
outbound WebSocket and the AI API. Traffic arriving at the callback endpoint
travels through the ALB, not directly to the task's public IP — the ECS task
security group blocks all direct inbound access.

---

## Step 7b — Set Up HTTPS with an Application Load Balancer

This step provisions an ALB with a TLS certificate so callback traffic from
workflow receivers is encrypted end-to-end. Skip this step if you are running
in fire-and-forget mode (no callbacks).

**Why an ALB and not Flask TLS directly?** Flask can serve TLS using
`ssl_context`, but that means managing certificate files, restarting on
renewal, and no connection buffering. An ALB handles all of this — free
auto-renewing certificates via ACM, HTTP → HTTPS redirect at the listener
level, and health checks that restart the ECS task if Flask stops responding.

### Get a second subnet (ALB requires at least two AZs)

```bash
SUBNET_ID_2=$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values=$VPC_ID \
  --query 'Subnets[1].SubnetId' \
  --output text)

echo "Subnet 2: $SUBNET_ID_2"
```

### Request a TLS certificate from ACM

ACM certificates are free and auto-renew. DNS validation is the easiest
method — ACM gives you a CNAME record to add to your domain's DNS.

```bash
CERT_ARN=$(aws acm request-certificate \
  --domain-name factobot.yourcompany.com \
  --validation-method DNS \
  --query 'CertificateArn' \
  --output text)

echo "Certificate ARN: $CERT_ARN"

# Check validation status (must be ISSUED before the ALB listener will work)
aws acm describe-certificate \
  --certificate-arn $CERT_ARN \
  --query 'Certificate.Status'
```

ACM outputs CNAME records you must add to your DNS provider. Once added,
validation typically completes within a few minutes. The status changes from
`PENDING_VALIDATION` to `ISSUED`.

> If your domain is in Route 53, you can automate DNS validation:
> ```bash
> aws acm describe-certificate --certificate-arn $CERT_ARN \
>   --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
> ```
> Then create a Route 53 record set with those values.

### Create the Application Load Balancer

```bash
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name factobot-alb \
  --subnets $SUBNET_ID $SUBNET_ID_2 \
  --security-groups $ALB_SG_ID \
  --scheme internet-facing \
  --type application \
  --query 'LoadBalancers[0].LoadBalancerArn' \
  --output text)

echo "ALB ARN: $ALB_ARN"

# Note your ALB's DNS name — you'll create a CNAME pointing here
aws elbv2 describe-load-balancers \
  --load-balancer-arns $ALB_ARN \
  --query 'LoadBalancers[0].DNSName' \
  --output text
```

### Create a target group

The target group tells the ALB how to reach the ECS tasks. Health checks
on `GET /health` confirm the Flask server is running before routing traffic.

```bash
TARGET_GROUP_ARN=$(aws elbv2 create-target-group \
  --name factobot-tg \
  --protocol HTTP \
  --port 3000 \
  --vpc-id $VPC_ID \
  --target-type ip \
  --health-check-path /health \
  --health-check-interval-seconds 30 \
  --healthy-threshold-count 2 \
  --unhealthy-threshold-count 3 \
  --query 'TargetGroups[0].TargetGroupArn' \
  --output text)

echo "Target group ARN: $TARGET_GROUP_ARN"
```

### Add the HTTPS listener (port 443)

This is where TLS termination happens. The ALB decrypts incoming HTTPS
traffic, then forwards it as plain HTTP to the ECS task on port 3000.
The ECS task security group blocks all direct inbound access so the
unencrypted internal hop is never reachable from the public internet.

```bash
aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTPS \
  --port 443 \
  --certificates CertificateArn=$CERT_ARN \
  --default-actions Type=forward,TargetGroupArn=$TARGET_GROUP_ARN
```

### Add the HTTP listener (port 80) — redirect to HTTPS

Any callback sender using plain HTTP is automatically redirected to HTTPS
by the ALB before the request ever reaches Flask. No code change needed.

```bash
aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTP \
  --port 80 \
  --default-actions \
    Type=redirect,\
    RedirectConfig="{Protocol=HTTPS,Port=443,StatusCode=HTTP_301}"
```

### Point your domain at the ALB

Create a CNAME record in your DNS provider:

```
factobot.yourcompany.com  →  factobot-alb-xxxx.us-east-1.elb.amazonaws.com
```

If your domain is in Route 53, use an Alias record instead of a CNAME
(Alias records are free and resolve faster):

```bash
aws route53 change-resource-record-sets \
  --hosted-zone-id YOUR_ZONE_ID \
  --change-batch '{
    "Changes": [{
      "Action": "CREATE",
      "ResourceRecordSet": {
        "Name": "factobot.yourcompany.com",
        "Type": "A",
        "AliasTarget": {
          "HostedZoneId": "Z35SXDOTRQ7X7K",
          "DNSName": "factobot-alb-xxxx.us-east-1.elb.amazonaws.com",
          "EvaluateTargetHealth": true
        }
      }
    }]
  }'
```

> The ALB hosted zone ID `Z35SXDOTRQ7X7K` is the fixed zone ID for
> `us-east-1` ALBs. Other regions have different IDs — check the
> [AWS documentation](https://docs.aws.amazon.com/general/latest/gr/elb.html).

### Update CALLBACK_BASE_URL

Now that you have a domain and HTTPS, update `CALLBACK_BASE_URL` in Secrets
Manager to your domain:

```bash
aws secretsmanager put-secret-value \
  --secret-id factobot/CALLBACK_BASE_URL \
  --secret-string "https://factobot.yourcompany.com"
```

Then force a redeployment so the new value takes effect:

```bash
aws ecs update-service \
  --cluster factobot-cluster \
  --service factobot-service \
  --force-new-deployment
```

### Verify HTTPS is working

```bash
curl -I https://factobot.yourcompany.com/health
```

Expected response:
```
HTTP/2 200
content-type: application/json
```

A `301` redirect on port 80 confirms the HTTP → HTTPS redirect is working:

```bash
curl -I http://factobot.yourcompany.com/health
# HTTP/1.1 301 Moved Permanently
# Location: https://factobot.yourcompany.com/health
```

---

## Step 8 — Verify the Deployment

Check that the task is running:

```bash
aws ecs list-tasks --cluster factobot-cluster

# Get details including the task status
aws ecs describe-tasks \
  --cluster factobot-cluster \
  --tasks $(aws ecs list-tasks --cluster factobot-cluster --query 'taskArns[0]' --output text)
```

Tail the logs to confirm the bot connected to Slack:

```bash
aws logs tail /ecs/factobot --follow
```

You should see output like:
```
Starting Factobot in Socket Mode...
⚡️ Bolt app is running!
```

Test it in Slack by sending a DM to your bot or running `/factobot`.

---

## Step 9 — Host Icons on S3

Create an S3 bucket for the bot's icon images. The bucket needs public read
access so Slack can fetch the icons server-side.

```bash
BUCKET_NAME=factobot-icons-$(date +%s)   # unique name
REGION=us-east-1

# Create the bucket
aws s3 mb s3://$BUCKET_NAME --region $REGION

# Disable Block Public Access (required before setting a public bucket policy)
aws s3api put-public-access-block \
  --bucket $BUCKET_NAME \
  --public-access-block-configuration \
    BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false

# Apply a bucket policy that allows public read on all objects
aws s3api put-bucket-policy \
  --bucket $BUCKET_NAME \
  --policy "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Principal\": \"*\",
      \"Action\": \"s3:GetObject\",
      \"Resource\": \"arn:aws:s3:::$BUCKET_NAME/*\"
    }]
  }"

echo "Bucket: $BUCKET_NAME"
```

Upload your icons:

```bash
aws s3 cp info-icon.png  s3://$BUCKET_NAME/info-icon.png
aws s3 cp ack-icon.png   s3://$BUCKET_NAME/ack-icon.png
aws s3 cp error-icon.png s3://$BUCKET_NAME/error-icon.png
```

The public URL for each icon follows this pattern:
```
https://{bucket-name}.s3.{region}.amazonaws.com/{filename}
```

For example:
```
https://factobot-icons-1234567890.s3.us-east-1.amazonaws.com/info-icon.png
```

Update these URLs in your task definition's `environment` section and redeploy.

---

## Redeploying After Code Changes

When you push new code, rebuild the image, push it to ECR, and force ECS to
restart the task with the new image.

```bash
# Rebuild and push
docker build -t factobot .
docker tag factobot:latest $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/factobot:latest
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/factobot:latest

# Force ECS to pull the new image and restart the task
aws ecs update-service \
  --cluster factobot-cluster \
  --service factobot-service \
  --force-new-deployment
```

ECS starts a new task with the new image, waits for it to reach a steady state,
then stops the old task — giving you a zero-downtime deployment.

---

## Setting Up Automatic Deploys with GitHub Actions

Add this workflow file to your repo to automatically build and deploy on every
push to `main`:

```yaml
# .github/workflows/deploy.yml
name: Deploy to ECS

on:
  push:
    branches: [main]

env:
  AWS_REGION:     us-east-1
  ECR_REPOSITORY: factobot
  ECS_CLUSTER:    factobot-cluster
  ECS_SERVICE:    factobot-service

jobs:
  deploy:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id:     ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region:            ${{ env.AWS_REGION }}

      - name: Login to ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build, tag, and push image
        env:
          ECR_REGISTRY: ${{ steps.login-ecr.outputs.registry }}
          IMAGE_TAG:    ${{ github.sha }}
        run: |
          docker build -t $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG .
          docker push $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG
          echo "image=$ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG" >> $GITHUB_OUTPUT

      - name: Force ECS redeployment
        run: |
          aws ecs update-service \
            --cluster ${{ env.ECS_CLUSTER }} \
            --service ${{ env.ECS_SERVICE }} \
            --force-new-deployment
```

Add `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` as secrets in your GitHub
repository settings (Settings → Secrets and variables → Actions). Create a
dedicated IAM user with ECR push and ECS update permissions for CI — do not
use your personal AWS credentials.

---

## Production Recommendations

### Conversation history persistence

The bot stores conversation history in memory, which is wiped every time the
ECS task restarts. Add **ElastiCache (Redis)** to persist history across restarts
and enable multiple task instances if needed in future:

1. Create an ElastiCache Redis cluster in the same VPC as your ECS tasks
2. Add the Redis endpoint as a secret in Secrets Manager
3. Uncomment the Redis implementation in `app/ai_client.py`
4. Add `redis` to `requirements.txt`
5. Update the security group to allow inbound traffic from the ECS task SG on port 6379

### Cost

For a single bot instance with minimal traffic, the monthly cost is roughly:

| Service | Cost |
|---|---|
| Fargate (0.25 vCPU, 0.5 GB, 24/7) | ~$11/month |
| Application Load Balancer | ~$16/month |
| ACM certificate | Free |
| ECR storage (small image) | <$0.50/month |
| Secrets Manager (4 secrets) | ~$1.60/month |
| CloudWatch Logs | ~$0.50/month |
| S3 (icons, negligible traffic) | <$0.01/month |
| **Total (with ALB + HTTPS)** | **~$30/month** |
| **Total (fire-and-forget, no ALB)** | **~$14/month** |

The ALB is the largest cost driver. If budget is a concern and you don't need
callbacks, omit Step 7b entirely and run in fire-and-forget mode.

ElastiCache (if added) starts at ~$13/month for the smallest Redis instance.

### Monitoring

Set up a CloudWatch alarm to alert you if the ECS task stops running:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name factobot-task-stopped \
  --alarm-description "Alert if Factobot ECS task count drops to zero" \
  --namespace AWS/ECS \
  --metric-name RunningTaskCount \
  --dimensions Name=ClusterName,Value=factobot-cluster Name=ServiceName,Value=factobot-service \
  --statistic Average \
  --period 60 \
  --evaluation-periods 2 \
  --threshold 1 \
  --comparison-operator LessThanThreshold \
  --alarm-actions arn:aws:sns:us-east-1:YOUR_ACCOUNT_ID:your-alert-topic
```

---

## Teardown

To stop all resources and avoid ongoing charges:

```bash
# Scale down the service (stops the running task)
aws ecs update-service \
  --cluster factobot-cluster \
  --service factobot-service \
  --desired-count 0

# Delete the ECS service
aws ecs delete-service \
  --cluster factobot-cluster \
  --service factobot-service

# Delete the ALB and its listeners (listeners are deleted automatically)
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN

# Delete the target group
aws elbv2 delete-target-group --target-group-arn $TARGET_GROUP_ARN

# Delete the security groups (must delete ALB SG after ALB is deleted)
aws ec2 delete-security-group --group-id $SG_ID
aws ec2 delete-security-group --group-id $ALB_SG_ID

# Delete the ACM certificate (optional — certificates are free)
aws acm delete-certificate --certificate-arn $CERT_ARN

# Delete the ECS cluster
aws ecs delete-cluster --cluster factobot-cluster

# Delete the ECR repository and all images
aws ecr delete-repository --repository-name factobot --force

# Delete the secrets
aws secretsmanager delete-secret --secret-id factobot/SLACK_BOT_TOKEN --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id factobot/SLACK_APP_TOKEN --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id factobot/ANTHROPIC_API_KEY --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id factobot/MAKE_WEBHOOK_URL --force-delete-without-recovery

# Delete the log group
aws logs delete-log-group --log-group-name /ecs/factobot

# Empty and delete the S3 icons bucket
aws s3 rm s3://$BUCKET_NAME --recursive
aws s3 rb s3://$BUCKET_NAME
```

# Cloud Run

Run executables on cloud instances with a single command.

## Quick Start

No installation required! Use `uvx` to run directly from GitHub.

If you already have your AWS credentials set up, you can try it out right away!

```bash
# Run on AWS EC2
uvx --from git+https://github.com/dacort/runbox-cloud cloud-run run --instance-type t3.micro <my_executable>
```

Behind the scenes, we create a VPC (if needed), all the supporting IAM roles, launch your instance, execute your code, and clean up the instance when done. The VPC and roles are left behind for faster subsequent runs.

If you want, you can also just shell into the instance.

```bash
# Interactive shell on an instance
uvx --from git+https://github.com/dacort/runbox-cloud cloud-run shell --instance-type t3.micro
```

## Need a test script?

Create a simple test script:

```bash
cat > test-it.sh << 'EOF'
#!/bin/sh

echo "==================================="
echo "      HELLO FROM RUNBOX.CLOUD"
echo "==================================="
echo

echo $(ec2-metadata --instance-type)
echo $(hostname)
echo

echo 👋
EOF

chmod +x test-it.sh
```

Then run it:

```bash
uvx --from git+https://github.com/dacort/runbox-cloud cloud-run run --instance-type t3.micro ./test-it.sh
```

## Commands

- `run` - Execute a file on a cloud instance (auto-cleanup)
- `shell` - Start an interactive shell session (auto-cleanup)

## Supported Clouds

### AWS
Default provider. Requires AWS credentials configured (`aws configure` or environment variables).

### DigitalOcean
Specify with `--provider digitalocean`. Requires `DIGITALOCEAN_TOKEN` environment variable and `doctl` command.

```bash
uvx --from git+https://github.com/dacort/runbox-cloud cloud-run run --provider digitalocean --instance-type s-2vcpu-4gb <my_executable>
```

## TODOS

- Allow user to specify a pre-existing VPC or IAM Role
- Better logging
- Partial deletion / just remove resources from config when deleted
- Better AWS session client
- Better depends attrs names than just _lower_
- Cascading deletes and dependencies
    - e.g. role can't be deleted without the instance profile being detached
- IPv6
- S3/SSM Endpoint
- Decide whether to delete my ID in VPC, or list and delete
- Add option to add a NAT Gateway if they want to burn money

It all starts [with a skeet](https://bsky.app/profile/alexmillerdb.bsky.social/post/3lnvz2fxac22p).

![I want to be able to just `run-on-cloud --instance i3en.2xlarge ./my_benchmark`](image.png)

## Let's make it happen

This is a two-parter that I don't quite know how I want it to work yet.

For example, one way to take it is to do something like this:

```bash
aws up s3/a-bucket-name ec2/m5.24xl
```

```bash
aws run ./benchmark
```

Alternatively, we can focus on the original use-case, which is just:

```bash
run-on-cloud --instance-type m5.24xl ./benchmark
```

Honestly, I think they can be both. 

- `up` is a bootstrap function
- `run` is a sync and wait function

## Process

### AWS

For AWS, a VPC, subnet, and security group is necessary. Some AWS accounts do not have a default VPC, so it's safest to create a dedicated set of resources for this that we can re-use. 

- Pre-requisites
    - VPC + Subnet(s)
    - Security Group with outbound 443 access for SSM
    - EBS Volume that we can optionally retain on termination
    - IAM role with `AmazonSSMManagedInstanceCore` managed policy attached

Once those are in place, we can easily create EC2 instances.

#### Manually bootstrap

- Create a basic VPC with public, private subnets, default outbound security group, and tagged with our project.

Users can opt-in to running their instance on a public subnet if they need outbound Internet access as we don't want to create a static resource (NAT Gateway) that continues to rack up costs.

```bash
aws ec2 create-vpc --cidr-block "10.122.0.0/16" --instance-tenancy "default" --tag-specifications '{"resourceType":"vpc","tags":[{"key":"Name","value":"cloudrun-vpc"}]}' 
```

![](console-vpc-creation.png)

## FAQ

- Why _not_ use CloudFormation/CDK/Terraform/etc

Infrastructure as Code is a well-established pattern and using tools like these can ease provisioning and maintenance burden. However, `cloudrun` aims to be as lightweight as possible, both in the resources it creates, but as as well as artifacts it leaves around and dependencies it takes on.

As an example for CloudFormation and CDK, an artifacts bucket needs to get created prior to being able to deploy anything. 

Similarly, Terraform requires a CLI install and stores additional state files for ongoing management.
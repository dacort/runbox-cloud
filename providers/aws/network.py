import logging

import boto3

from .base import AWSResource

logger = logging.getLogger(__name__)


class VPC(AWSResource):
    """Create a public VPC with an S3 gateway, internet gateway, and default security group.

    The subnet does not have public IPs by default, but can be assigned per instance.
    The default security group allows all outbound traffic, but no inbound traffic.

    No NAT gateway is used to keep costs low.
    """

    resource_type = "network"
    retain_by_default = True

    def __init__(self, cidr_block: str = "10.120.0.0/16", **kwargs):
        self.cidr_block = cidr_block
        super().__init__(**kwargs)

    def _create(self) -> str:
        ec2 = boto3.client("ec2")

        # Create VPC
        vpc_response = ec2.create_vpc(
            CidrBlock=self.cidr_block,
            TagSpecifications=[
                {
                    "ResourceType": "vpc",
                    "Tags": [
                        {"Key": "Name", "Value": self.name or "cloudrun-preview-vpc"},
                        {"Key": "cloudrun-version", "Value": "v0.0.1"},
                    ],
                }
            ],
        )
        vpc_id = vpc_response["Vpc"]["VpcId"]

        # Enable DNS support
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})

        # TODO: Quick validation to make sure the VPC was created
        # TODO: Create an S3 endpoint - they're free, so why not?
        # But right now the EC2 instance profile doesn't have any access to S3.

        # Create internet gateway
        igw_response = ec2.create_internet_gateway()
        igw_id = igw_response["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        # Create subnet
        subnet_response = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.120.1.0/24")
        subnet_id = subnet_response["Subnet"]["SubnetId"]

        # Create route tables to allow internet access
        route_table_response = ec2.create_route_table(VpcId=vpc_id)
        route_table_id = route_table_response["RouteTable"]["RouteTableId"]
        ec2.create_route(
            RouteTableId=route_table_id,
            DestinationCidrBlock="0.0.0.0/0",
            GatewayId=igw_id,
        )
        ec2.associate_route_table(RouteTableId=route_table_id, SubnetId=subnet_id)

        # Create security group
        sg_response = ec2.create_security_group(
            GroupName=f"cloudrun-{vpc_id}",
            Description="Default security group",
            VpcId=vpc_id,
        )
        sg_id = sg_response["GroupId"]

        # Update config with all VPC details
        self._config.update(
            {
                "vpc_id": vpc_id,
                "security_group_id": sg_id,
                "subnet_ids": [subnet_id],
                "internet_gateway_id": igw_id,
                "cidr_block": self.cidr_block,
            }
        )

        return vpc_id

    def _destroy(self):
        ec2 = boto3.client("ec2")

        # Detach and delete internet gateway
        if self._config.get("internet_gateway_id"):
            ec2.detach_internet_gateway(
                InternetGatewayId=self._config["internet_gateway_id"],
                VpcId=self._config["vpc_id"],
            )
            ec2.delete_internet_gateway(
                InternetGatewayId=self._config["internet_gateway_id"]
            )
            self._config.pop("internet_gateway_id")

        # Delete subnets
        for subnet_id in self._config.get("subnet_ids", []):
            ec2.delete_subnet(SubnetId=subnet_id)
            self._config["subnet_ids"].remove(subnet_id)

        # Delete security group
        if self._config.get("security_group_id"):
            ec2.delete_security_group(GroupId=self._config["security_group_id"])
            self._config.pop("security_group_id")

        # Delete route tables
        route_tables = ec2.describe_route_tables(
            Filters=[
                {"Name": "vpc-id", "Values": [self._config["vpc_id"]]},
                # {"Name": "association.main", "Values": ["true"]},
            ]
        )
        for rt in route_tables.get("RouteTables", []):
            if any([a.get("Main") for a in rt.get("Associations", [])]):
                continue
            logger.info(f"Deleting route table {rt['RouteTableId']}")
            ec2.delete_route_table(RouteTableId=rt["RouteTableId"])

        # Delete VPC
        ec2.delete_vpc(VpcId=self._config["vpc_id"])

    def _exists(self) -> bool:
        if not self._config.get("vpc_id"):
            return False

        ec2 = boto3.client("ec2")
        try:
            response = ec2.describe_vpcs(VpcIds=[self._config["vpc_id"]])
            return len(response["Vpcs"]) > 0
        except:
            return False
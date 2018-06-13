from troposphere import Template
from troposphere import ec2
from troposphere import autoscaling
from troposphere import elasticloadbalancingv2 as elb
from troposphere import Ref, GetAtt, FindInMap, GetAZs



def addMapping(template):
    template.add_mapping("RegionMap", {
        "us-east-1": {"AMI": "ami-97785bed"},
    })

def main():
    t = Template("A template for trying out DevOps tools.")

    addMapping(t)

    vpc = ec2.VPC(
        "DevOpsVPC", 
        CidrBlock="10.1.0.0/16"
    )

    t.add_resource(vpc)

    vpc_id = Ref(vpc)

    pub_subnet = ec2.Subnet(
        "publicNginxSubnet", 
        t, 
        AvailabilityZone="us-east-1a",
        CidrBlock="10.1.0.0/24",
        MapPublicIpOnLaunch=True,
        VpcId=vpc_id,
    )
    pub_subnet_id = Ref(pub_subnet)

    priv_subnet = ec2.Subnet(
        "privateFlaskSubnet", 
        t,
        AvailabilityZone="us-east-1b",
        CidrBlock="10.1.1.0/24",
        MapPublicIpOnLaunch=True,
        VpcId=vpc_id,
    )

    # NETWORKING
    igw = ec2.InternetGateway("internetGateway")
    route_table = ec2.RouteTable(
        "subnetRouteTable", 
        t,
        VpcId=vpc_id
    )
    route_table_id = Ref(route_table)
    internet_route = ec2.Route(
        "routToInternet",
        t,
        DependsOn=igw,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=Ref(igw),
        RouteTableId=route_table_id
    )

    priv_subnet_route_assoc = ec2.SubnetRouteTableAssociation(
        "privateSubnetRouteAssociation",
        t,
        RouteTableId=route_table_id,
        SubnetId=Ref(priv_subnet)
    )
    pub_subnet_route_assoc = ec2.SubnetRouteTableAssociation(
        "publicSubnetRouteAssociation",
        t,
        RouteTableId=route_table_id,
        SubnetId=Ref(pub_subnet)
    )

    http_ingress = {
        "CidrIp": "0.0.0.0/0",
        "Description": "Allow HTTP traffic in from internet.",
        "IpProtocol": "tcp",
        "FromPort": 80,
        "ToPort": 80,
        "VpcId": vpc_id
    }
    ssh_ingress = {
        "CidrIp": "0.0.0.0/0",
        "Description": "Allow SSH traffic in from internet.",
        "IpProtocol": "tcp",
        "FromPort": 22,
        "ToPort": 22,
        "VpcId": vpc_id
    }

    elb_sg = ec2.SecurityGroup(
        "elbSecurityGroup", 
        t,
        GroupName="WebGroup",
        GroupDescription="Allow web traffic in from internet to ELB",
        SecurityGroupIngress=[
            http_ingress
        ])
    ssh_sg = ec2.SecurityGroup(
        "sshSecurityGroup",
        t,
        GroupName="SSHGroup",
        GroupDescription="Allow SSH traffic in from internet",
        SecurityGroupIngress=[
            ssh_ingress
        ]
    )
    ssh_sg_id = Ref(ssh_sg)
    elb_sg_id = Ref(elb_sg)

    autoscale_ingress = {
        "SourceSecurityGroupId":Ref(elb_sg_id),
        "Description": "Allow web traffic in from ELB",
        "FromPort": 80,
        "ToPort": 80
    }
    autoscale_sg = ec2.SecurityGroup(
        "nginxAutoscaleSG",
        t,
        GroupName="AutoscaleGroup",
        GroupDescription="Allow web traffic in from elb on port 80",
        SecurityGroupIngress=[
            autoscale_ingress
        ]
    )
    autoscale_sg_id = Ref(autoscale_sg)



    nginx_elb = elb.LoadBalancer(
        "nginxElb",
        t,
        Name="nginxElb",
        Subnets=[pub_subnet_id],
        SecurityGroups=[elb_sg_id]
    )

    nginx_target_group = elb.TargetGroup(
        "nginxTargetGroup", 
        t,
        HealthCheckIntervalSeconds=30,
        HealthCheckPath="/health.html",
        HealthCheckPort=80,
        HealthCheckProtocol="HTTP",
        HealthCheckTimeoutSeconds=90,
        HealthyThresholdCount=4,
        UnhealthyThresholdCount=3,
        Matcher=elb.Matcher(HttpCode="200"),
        Name="Nginx Target Group",
        Port=80,
        Protocol="HTTP",
        VpcId=vpc_id
    )

    nginx_listener = elb.Listener(
        "nginxListener",
        t,
        LoadBalancerArn=Ref(nginx_elb),
        DefaultActions=[
            elb.Action("forwardAction",
                TargetGroupArn=Ref(nginx_target_group),
                Type="forward"
            )
        ],
        Port=80,
        Protocol="HTTP"
    )

    #TODO: Add user data to install nginx and ansible 
    nginx_launch_config = autoscaling.LaunchConfiguration(
        "webLaunchConfig",
        t,
        ImageId=FindInMap("RegionMap", Ref("AWS::Region"), "AMI"), #TODO: Remove magic string
        SecurityGroups=[ssh_sg_id, autoscale_sg_id],
        InstanceType="t2.micro",
        BlockDeviceMappings= [{
            "DeviceName": "/dev/sdk",
            "Ebs": {"VolumeSize": "10"}
        }]
    )

    nginx_autoscaler = autoscaling.AutoScalingGroup(
        "nginxAutoScaler",
        LaunchConfigurationName=Ref(nginx_launch_config),
        MinSize="2",
        MaxSize="2",
        AvailabilityZones=GetAZs("")
    )

    print(t.to_yaml())

if __name__ == "__main__":
    main()
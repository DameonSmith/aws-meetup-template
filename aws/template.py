from troposphere import Template
from troposphere import ec2
from troposphere import autoscaling
from troposphere import elasticloadbalancingv2 as elb
from troposphere import Ref, GetAtt, FindInMap, GetAZs, Base64, Join, Output



def addMapping(template):
    template.add_mapping("RegionMap", {
        "us-east-1": {"AMI": "ami-a4dc46db"},
    })

def main():
    t = Template("A template to create a load balanced autoscaled nginx flask deployment using ansible.")

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
    priv_subnet_id = Ref(priv_subnet)

    # NETWORKING
    igw = ec2.InternetGateway("internetGateway", t)

    gateway_to_internet = ec2.VPCGatewayAttachment(
        "GatewayToInternet",
        t,
        VpcId=vpc_id,
        InternetGatewayId=Ref(igw)
    )

    route_table = ec2.RouteTable(
        "subnetRouteTable", 
        t,
        VpcId=vpc_id
    )

    route_table_id = Ref(route_table)
    internet_route = ec2.Route(
        "routToInternet",
        t,
        DependsOn=gateway_to_internet,
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
    }
    ssh_ingress = {
        "CidrIp": "0.0.0.0/0",
        "Description": "Allow SSH traffic in from internet.",
        "IpProtocol": "tcp",
        "FromPort": 22,
        "ToPort": 22,
    }

    elb_sg = ec2.SecurityGroup(
        "elbSecurityGroup", 
        t,
        GroupName="WebGroup",
        GroupDescription="Allow web traffic in from internet to ELB",
        VpcId=vpc_id,
        SecurityGroupIngress=[
            http_ingress
        ])
    ssh_sg = ec2.SecurityGroup(
        "sshSecurityGroup",
        t,
        GroupName="SSHGroup",
        GroupDescription="Allow SSH traffic in from internet",
        VpcId=vpc_id,
        SecurityGroupIngress=[
            ssh_ingress
        ]
    )
    ssh_sg_id = Ref(ssh_sg)
    elb_sg_id = Ref(elb_sg)

    autoscale_ingress = {
        "SourceSecurityGroupId": elb_sg_id,
        "Description": "Allow web traffic in from ELB",
        "IpProtocol": "tcp",
        "FromPort": 80,
        "ToPort": 80
    }
    autoscale_sg = ec2.SecurityGroup(
        "nginxAutoscaleSG",
        t,
        GroupName="AutoscaleGroup",
        GroupDescription="Allow web traffic in from elb on port 80",
        VpcId=vpc_id,
        SecurityGroupIngress=[
            autoscale_ingress
        ]
    )
    autoscale_sg_id = Ref(autoscale_sg)



    nginx_elb = elb.LoadBalancer(
        "nginxElb",
        t,
        Name="nginxElb",
        Subnets=[pub_subnet_id, priv_subnet_id],
        SecurityGroups=[elb_sg_id]
    )

    nginx_target_group = elb.TargetGroup(
        "nginxTargetGroup", 
        t,
        DependsOn=nginx_elb,
        HealthCheckPath="/health",
        HealthCheckPort=80,
        HealthCheckProtocol="HTTP",
        Matcher=elb.Matcher(HttpCode="200"),
        Name="NginxTargetGroup",
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

    # Everything after su -u ubuntu is one command
    lc_user_data = Base64(Join("\n",
    [
        "#!/bin/bash",
        "apt-add-repository -y ppa:ansible/ansible",
        "apt-get update && sudo apt-get -y upgrade",
        "apt-get -y install git",
        "apt-get -y install ansible",
        "cd /home/ubuntu/",
        "sudo -H -u ubuntu bash -c '"
        "export LC_ALL=C.UTF-8 && "
        "export LANG=C.UTF-8 && "
        "ansible-pull -U https://github.com/DameonSmith/aws-meetup-ansible.git --extra-vars \"user=ubuntu\"'"
    ]))

    nginx_launch_config = autoscaling.LaunchConfiguration(
        "webLaunchConfig",
        t,
        ImageId=FindInMap("RegionMap", Ref("AWS::Region"), "AMI"), #TODO: Remove magic string
        SecurityGroups=[ssh_sg_id, autoscale_sg_id],
        InstanceType="t2.micro",
        BlockDeviceMappings= [{
            "DeviceName": "/dev/sdk",
            "Ebs": {"VolumeSize": "10"}
        }],
        UserData= lc_user_data,
        KeyName="advanced-cfn"
    )

    nginx_autoscaler = autoscaling.AutoScalingGroup(
        "nginxAutoScaler",
        t,
        LaunchConfigurationName=Ref(nginx_launch_config),
        MinSize="2",
        MaxSize="2",
        VPCZoneIdentifier=[priv_subnet_id, pub_subnet_id],
        TargetGroupARNs= [Ref(nginx_target_group)]
    )

    t.add_output([
        Output(
            "ALBDNS",
            Description="The DNS name for the application load balancer.",
            Value=GetAtt(nginx_elb, "DNSName")
        )
    ])

    print(t.to_yaml())

if __name__ == "__main__":
    main()
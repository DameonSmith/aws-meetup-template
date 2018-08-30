from troposphere import Template
from troposphere import ec2
from troposphere import autoscaling
from troposphere import s3
from troposphere import codebuild, codedeploy
from troposphere import iam
from troposphere import elasticloadbalancingv2 as elb
from troposphere import Ref, GetAtt, FindInMap, GetAZs, Base64, Join, Output, Parameter
from awacs import aws
from awacs import logs as aws_logs
from awacs import s3 as aws_s3
from awacs import autoscaling as aws_autoscaling



def addMapping(template):
    template.add_mapping("RegionMap", {
        "us-east-1": {"AMI": "ami-a4dc46db"},
    })

def main():
    t = Template("A template to create a load balanced autoscaled Web flask deployment using ansible.")

    addMapping(t)

    ### VPC CONFIGURATION ###
    vpc = ec2.VPC(
        "MainVPC",
        CidrBlock="10.1.0.0/16"
    )

    t.add_resource(vpc)

    vpc_id = Ref(vpc)

    subnet_1 = ec2.Subnet(
        "WebAppSubnet1",
        t,
        AvailabilityZone="us-east-1a",
        CidrBlock="10.1.0.0/24",
        MapPublicIpOnLaunch=True,
        VpcId=vpc_id,
    )
    subnet_1_id = Ref(subnet_1)

    subnet_2 = ec2.Subnet(
        "WebAppSubnet2",
        t,
        AvailabilityZone="us-east-1b",
        CidrBlock="10.1.1.0/24",
        MapPublicIpOnLaunch=True,
        VpcId=vpc_id,
    )
    subnet_2_id = Ref(subnet_2)

    ### NETWORKING ###
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
        "routeToInternet",
        t,
        DependsOn=gateway_to_internet,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=Ref(igw),
        RouteTableId=route_table_id
    )
    subnet_1_route_assoc = ec2.SubnetRouteTableAssociation(
        "Subnet1RouteAssociation",
        t,
        RouteTableId=route_table_id,
        SubnetId=Ref(subnet_1)
    )
    subnet_2_route_assoc = ec2.SubnetRouteTableAssociation(
        "Subnet2RouteAssociation",
        t,
        RouteTableId=route_table_id,
        SubnetId=Ref(subnet_2)
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
    elb_sg_id = Ref(elb_sg)
    ssh_sg_id = Ref(ssh_sg)

    autoscale_ingress = {
        "SourceSecurityGroupId": elb_sg_id,
        "Description": "Allow web traffic in from ELB",
        "IpProtocol": "tcp",
        "FromPort": 80,
        "ToPort": 80
    }
    autoscale_sg = ec2.SecurityGroup(
        "WebAutoscaleSG",
        t,
        GroupName="AutoscaleGroup",
        GroupDescription="Allow web traffic in from elb on port 80",
        VpcId=vpc_id,
        SecurityGroupIngress=[
            autoscale_ingress
        ]
    )
    autoscale_sg_id = Ref(autoscale_sg)

    # BUCKETS
    app_bucket = s3.Bucket(
        "CodeDeployApplicationBucket",
        t,
    )

    ### LOAD BALANCING ###
    Web_elb = elb.LoadBalancer(
        "WebElb",
        t,
        Name="WebElb", # TODO: Fix for name conflict
        Subnets=[subnet_1_id, subnet_2_id],
        SecurityGroups=[elb_sg_id]
    )

    Web_target_group = elb.TargetGroup(
        "WebTargetGroup",
        t,
        DependsOn=Web_elb,
        HealthCheckPath="/health",
        HealthCheckPort=80,
        HealthCheckProtocol="HTTP",
        Matcher=elb.Matcher(HttpCode="200"),
        Name="NginxTargetGroup",
        Port=80,
        Protocol="HTTP",
        VpcId=vpc_id
    )

    Web_listener = elb.Listener(
        "WebListener",
        t,
        LoadBalancerArn=Ref(Web_elb),
        DefaultActions=[
            elb.Action("forwardAction",
                TargetGroupArn=Ref(Web_target_group),
                Type="forward"
            )
        ],
        Port=80,
        Protocol="HTTP"
    )

    ### AUTOSCALING ###
    # Everything after sudo -u ubuntu is one command
    # The sudo command is required to properly set file permissions when
    # running the ansible script as it assumes running from non root user
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

    web_instance_role = iam.Role(
        "webInstanceCodeDeployRole",
        t,
        AssumeRolePolicyDocument={
            'Statement': [{
                'Effect': 'Allow',
                'Principal': {
                    'Service': 'ec2.amazonaws.com'
                },
                'Action': 'sts:AssumeRole'
            }]
        },
        Policies=[
            iam.Policy(
                PolicyName="CodeDeployS3Policy",
                PolicyDocument=aws.Policy(
                    Version='2012-10-17',
                    Statement=[
                        aws.Statement(
                            Sid='CodeDeployS3',
                            Effect=aws.Allow,
                            Action=[
                                aws_s3.PutObject,
                                aws_s3.GetObject,
                                aws_s3.GetObjectVersion,
                                aws_s3.DeleteObject,
                                aws_s3.ListObjects,
                                aws_s3.ListBucket,
                                aws_s3.ListBucketVersions,
                                aws_s3.ListAllMyBuckets,
                                aws_s3.ListMultipartUploadParts,
                                aws_s3.ListBucketMultipartUploads,
                                aws_s3.ListBucketByTags,
                            ],
                            Resource=[
                                GetAtt(app_bucket, 'Arn'),
                                Join('', [
                                    GetAtt(app_bucket, 'Arn'),
                                    '/*',
                                ]),
                                "arn:aws:s3:::aws-codedeploy-us-east-2/*",
                                "arn:aws:s3:::aws-codedeploy-us-east-1/*",
                                "arn:aws:s3:::aws-codedeploy-us-west-1/*",
                                "arn:aws:s3:::aws-codedeploy-us-west-2/*",
                                "arn:aws:s3:::aws-codedeploy-ca-central-1/*",
                                "arn:aws:s3:::aws-codedeploy-eu-west-1/*",
                                "arn:aws:s3:::aws-codedeploy-eu-west-2/*",
                                "arn:aws:s3:::aws-codedeploy-eu-west-3/*",
                                "arn:aws:s3:::aws-codedeploy-eu-central-1/*",
                                "arn:aws:s3:::aws-codedeploy-ap-northeast-1/*",
                                "arn:aws:s3:::aws-codedeploy-ap-northeast-2/*",
                                "arn:aws:s3:::aws-codedeploy-ap-southeast-1/*",
                                "arn:aws:s3:::aws-codedeploy-ap-southeast-2/*",
                                "arn:aws:s3:::aws-codedeploy-ap-south-1/*",
                                "arn:aws:s3:::aws-codedeploy-sa-east-1/*",
                            ]
                        )
                    ]
                )
            )
        ]
    )

    web_instance_profile = iam.InstanceProfile(
        "webInstanceProfile",
        t,
        Path='/',
        Roles=[Ref(web_instance_role)],
    )

    Web_launch_config = autoscaling.LaunchConfiguration(
        "webLaunchConfig",
        t,
        ImageId=FindInMap("RegionMap", Ref("AWS::Region"), "AMI"), # TODO: Remove magic string
        SecurityGroups=[ssh_sg_id, autoscale_sg_id],
        IamInstanceProfile=Ref(web_instance_profile),
        InstanceType="t2.micro",
        BlockDeviceMappings= [{
            "DeviceName": "/dev/sdk",
            "Ebs": {"VolumeSize": "10"}
        }],
        UserData= lc_user_data,
        KeyName="advanced-cfn",
    )

    Web_autoscaler = autoscaling.AutoScalingGroup(
        "WebAutoScaler",
        t,
        LaunchConfigurationName=Ref(Web_launch_config),
        MinSize="2", # TODO: Change to parameter
        MaxSize="2",
        VPCZoneIdentifier=[subnet_2_id, subnet_1_id],
        TargetGroupARNs= [Ref(Web_target_group)]
    )

    t.add_output([
        Output(
            "ALBDNS",
            Description="The DNS name for the application load balancer.",
            Value=GetAtt(Web_elb, "DNSName")
        )
    ])


    # DEVTOOLS CONFIG
    codebuild_service_role = iam.Role(
        "CMSCodeBuildServiceRole",
        t,
        AssumeRolePolicyDocument={
            'Statement': [{
                'Effect': 'Allow',
                'Principal': {
                    'Service': ['codebuild.amazonaws.com']
                },
                'Action': ['sts:AssumeRole']
            }]
        },
        Policies=[
            iam.Policy(
                PolicyName="CloudWatchLogsPolicy",
                PolicyDocument=aws.Policy(
                    Version="2012-10-17",
                    Statement=[
                        aws.Statement(
                            Sid='logs',
                            Effect=aws.Allow,
                            Action=[
                                aws_logs.CreateLogGroup,
                                aws_logs.CreateLogStream,
                                aws_logs.PutLogEvents
                            ],
                            Resource=['*']
                        )
                    ]
                )
            ),
            iam.Policy(
                PolicyName="s3AccessPolicy",
                PolicyDocument=aws.Policy(
                    Version="2012-10-17",
                    Statement=[
                        aws.Statement(
                            Sid='codebuilder',
                            Effect=aws.Allow,
                            Action=[
                                aws_s3.PutObject,
                                aws_s3.GetObject,
                                aws_s3.GetObjectVersion,
                                aws_s3.DeleteObject
                            ],
                            Resource=[
                                GetAtt(app_bucket, 'Arn'),
                                Join('', [
                                    GetAtt(app_bucket, 'Arn'),
                                    '/*',
                                ])
                            ]
                        )
                    ]
                )
            )
        ]
    )


    github_repo = Parameter(
        "GithubRepoLink",
        Description="Name of the repository you wish to connect to codebuild.",
        Type="String"
    )

    artifact_key = Parameter(
        "ArtifactKey",
        Description="The key for the artifact that codebuild creates.",
        Type="String"
    )

    t.add_parameter(github_repo)
    t.add_parameter(artifact_key)


    cms_code_build_project = codebuild.Project(
        "CMSBuild",
        t,
        Name="CMS-Build",
        Artifacts=codebuild.Artifacts(
            Location=Ref(app_bucket),
            Name=Ref(artifact_key),
            NamespaceType="BUILD_ID",
            Type="S3",
            Packaging="ZIP"
        ),
        Description="Code build for CMS",
        Environment=codebuild.Environment(
            ComputeType="BUILD_GENERAL1_SMALL",
            Image="aws/codebuild/python:3.6.5",
            Type="LINUX_CONTAINER",
        ),
        ServiceRole=GetAtt(codebuild_service_role, 'Arn'),
        Source=codebuild.Source(
            "CMSSourceCode",
            Auth=codebuild.SourceAuth(
                "GitHubAuth",
                Type="OAUTH"
            ),
            Location=Ref(github_repo),
            Type="GITHUB"
        ),
        Triggers=codebuild.ProjectTriggers(
            Webhook=True
        )
    )


    codedeploy_service_role = iam.Role(
        "CMSDeploymentGroupServiceRole",
        t,
        AssumeRolePolicyDocument={
            'Statement': [{
                'Effect': 'Allow',
                'Principal': {
                    'Service': ['codedeploy.amazonaws.com']
                },
                'Action': ['sts:AssumeRole']
            }]
        },
        Policies=[
            iam.Policy(
                PolicyName="CloudWatchLogsPolicy",
                PolicyDocument=aws.Policy(
                    Version="2012-10-17",
                    Statement=[
                        aws.Statement(
                            Sid='logs',
                            Effect=aws.Allow,
                            Action=[
                                aws_logs.CreateLogGroup,
                                aws_logs.CreateLogStream,
                                aws_logs.PutLogEvents
                            ],
                            Resource=['*']
                        )
                    ]
                )
            ),
            iam.Policy(
                PolicyName="s3AccessPolicy",
                PolicyDocument=aws.Policy(
                    Version="2012-10-17",
                    Statement=[
                        aws.Statement(
                            Sid='codebuilder',
                            Effect=aws.Allow,
                            Action=[
                                aws_s3.PutObject,
                                aws_s3.GetObject,
                                aws_s3.GetObjectVersion,
                                aws_s3.DeleteObject
                            ],
                            Resource=[
                                GetAtt(app_bucket, 'Arn'),
                                Join('', [
                                    GetAtt(app_bucket, 'Arn'),
                                    '/*'
                                ])
                            ]
                        )
                    ]
                )
            ),
            iam.Policy(
                PolicyName="autoscalingAccess",
                PolicyDocument=aws.Policy(
                    Version="2012-10-17",
                    Statement=[
                        aws.Statement(
                            Sid='codebuilder',
                            Effect=aws.Allow,
                            Action=[
                                aws.Action('autoscaling', '*'),
                                aws.Action('elasticloadbalancing', '*')
                            ],
                            Resource=[
                                '*'
                            ]
                        )
                    ]
                )
            )
        ]
    )

    cms_codedeploy_application = codedeploy.Application(
        "CMSCodeDeployApplication",
        t,
    )


    cms_deployment_group = codedeploy.DeploymentGroup(
        "CMSDeploymentGroup",
        t,
        DependsOn=[cms_codedeploy_application],
        ApplicationName=Ref(cms_codedeploy_application),
        AutoScalingGroups=[Ref(Web_autoscaler)],
        LoadBalancerInfo=codedeploy.LoadBalancerInfo(
            "CodeDeployLBInfo",
            TargetGroupInfoList=[
                    codedeploy.TargetGroupInfoList(
                        "WebTargetGroup",
                       Name=GetAtt(Web_target_group, "TargetGroupName")
                    )
            ]
        ),
        ServiceRoleArn=GetAtt(codedeploy_service_role, 'Arn')
    )

    print(t.to_yaml())

if __name__ == "__main__":
    main()

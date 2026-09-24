// Jenkinsfile for HD_BaseMap_Tasks_Dump (Python replacement)
// Replaces FME-based HD_BeseMap_Tasks_Dump_v2 job
// Target: Windows node (adp-lodz-cloud-wind)
//
// This pipeline:
//   1. Checks out code from GitHub repository
//   2. Sets up a Python virtual environment with required packages
//   3. Runs issue_tracker_dump.py to query PostGIS and export GeoPackage
//   4. Archives output.zip as a build artifact
//
// Repository structure expected:
//   Jenkinsfile
//   issue_tracker_dump.py
//   requirements.txt

pipeline {
    agent {
        label 'adp-lodz-cloud-wind'  // Same Windows node label as the FME job
    }

    parameters {
        string(
            name: 'ProjectName',
            defaultValue: 'SWE_Sample_Motorways_ARC1',
            description: 'Project label to filter issues (e.g. SWE_Sample_Motorways_ARC1)'
        )
        choice(
            name: 'UseSpatialFilter',
            choices: ['no', 'yes'],
            description: 'Use spatial WKT filter instead of label filter'
        )
        string(
            name: 'WktFile',
            defaultValue: 'wkt.wkt',
            description: 'Path to WKT file for spatial filtering (only used when UseSpatialFilter=yes)'
        )
    }

    environment {
        // All DB connection details come from a single Azure Key Vault secret.
        // No hardcoded host/port/user/database — everything is in kv-issue-tracker.
    }

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {
        stage('Checkout') {
            steps {
                echo "Processing project: ${params.ProjectName}"
                // If configured as "Pipeline script from SCM", Jenkins
                // auto-checks out the repo before this stage runs.
                // The explicit checkout below is a safety net — it's
                // a no-op when auto-checkout already happened.
                checkout scm
            }
        }

        stage('Setup Python') {
            steps {
                bat '''
                    echo Setting up Python virtual environment...
                    python -m venv .venv
                    call .venv\\Scripts\\activate.bat
                    python -m pip install --upgrade pip
                    pip install -r requirements.txt
                '''
            }
        }

        stage('Dump Tasks') {
            steps {
                // Fetch PGPASSWORD from Azure Key Vault and run the dump
                // within the same closure so the secret stays scoped & masked
                // Key Vault: kv-adp-hd-contrib (subscription: Maps MCP ADP Prod)
                // Secret:    kv-issue-tracker
                azureKeyVault(
                    keyVaultURL: 'https://kv-adp-hd-contrib.vault.azure.net',
                    secrets: [
                        [secretType: 'Secret', name: 'kv-issue-tracker', envVariable: 'ISSUE_TRACKER_SECRET']
                    ]
                ) {
                    script {
                        def wktArg = ''
                        if (params.UseSpatialFilter == 'yes') {
                            wktArg = "--wkt-file \"${params.WktFile}\""
                        }
                        bat """
                            call .venv\\Scripts\\activate.bat
                            python issue_tracker_dump.py ^
                                --project-name "${params.ProjectName}" ^
                                --output output.zip ^
                                ${wktArg}
                        """
                    }
                }
            }
        }
    }

    post {
        success {
            archiveArtifacts artifacts: 'output.zip', fingerprint: true
            echo 'Translation was SUCCESSFUL'
        }
        failure {
            echo 'Translation FAILED'
        }
        always {
            cleanWs()
        }
    }
}
